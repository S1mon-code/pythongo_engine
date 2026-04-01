"""DailyV9 — ROC Momentum + OBV EMA Trend (铁矿石日线做多)

QBase_v2 策略: strong_trend_long_I_daily_v9 (TrendMediumV9)
信号逻辑:
  ROC > 0 AND OBV EMA上升 → 做多信号, 强度 = min(1.0, |ROC|/5)
  ROC < 0 或 OBV不确认    → 无信号
仓位: Vol Targeting 每日重算 + Carver 10% buffer
止损: 移动止损 + 2%权益硬止损 + Portfolio Stops

部署: 复制到 Windows 无限易 pyStrategy/self_strategy/
"""
import math
import os
import json
import time
import hashlib
import base64
import hmac
import threading
import queue
from datetime import datetime, timedelta

import numpy as np
import requests

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator


# ═══════════════════════════════════════════════════════════════
# Section 1: CONFIG
# ═══════════════════════════════════════════════════════════════

# 合约参数 (铁矿石)
MULTIPLIER = 100
TICK_SIZE = 0.5

# 策略指标参数 (QBase默认值, best_params={})
ROC_PERIOD = 20
OBV_EMA_PERIOD = 30
WARMUP = 70

# Vol Targeting
TARGET_VOL = 0.15
VOL_ATR_PERIOD = 20
ANNUAL_FACTOR = 252            # Daily bars per year

# Forecast (单策略, 无blending)
FORECAST_SCALAR = 10.0         # raw signal [0,1] × 10 → forecast [0,10]
FORECAST_CAP = 20.0

# Carver Buffer
BUFFER_FRACTION = 0.10
MIN_TRADE_SIZE = 1

# 止损
TRAILING_PCT = 2.0             # 移动止损 2%
HARD_STOP_EQUITY_PCT = 0.02    # 账户权益 2% 硬止损
STOP_WARNING = -0.10
STOP_REDUCE = -0.15
STOP_CIRCUIT = -0.20
STOP_DAILY = -0.05

# Chandelier Exit
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 2.5

# 飞书
FEISHU_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/a6aeb603-3d9f-40b5-a5a9-8f0a9cd3bf71"
FEISHU_SECRET = ""
FEISHU_ENABLED = True

# 换月提醒 (天数)
ROLLOVER_WARN_DAYS = 15
ROLLOVER_URGENT_DAYS = 5

# 状态文件路径
STATE_DIR = "./state"


# ═══════════════════════════════════════════════════════════════
# Section 2: INDICATORS (从QBase原样移植, 纯numpy)
# ═══════════════════════════════════════════════════════════════

def ema(data, period):
    """EMA seeded with data[0], no NaN. (QBase trend/ema.py)"""
    n = len(data)
    if n == 0:
        return np.array([], dtype=np.float64)
    alpha = 2.0 / (period + 1)
    out = np.empty(n, dtype=np.float64)
    out[0] = data[0]
    for i in range(1, n):
        out[i] = alpha * data[i] + (1.0 - alpha) * out[i - 1]
    return out


def rate_of_change(closes, period=12):
    """ROC: (close / close_n_ago - 1) × 100. (QBase momentum/roc.py)"""
    if closes.size == 0:
        return np.array([], dtype=float)
    if closes.size <= period:
        return np.full(closes.size, np.nan)
    roc = np.full(closes.size, np.nan)
    roc[period:] = (closes[period:] / closes[:-period] - 1.0) * 100.0
    return roc


def obv(closes, volumes):
    """On-Balance Volume. (QBase volume/obv.py)"""
    if closes.size == 0:
        return np.array([], dtype=np.float64)
    closes = closes.astype(np.float64)
    volumes = volumes.astype(np.float64)
    direction = np.zeros(len(closes), dtype=np.float64)
    direction[1:] = np.sign(np.diff(closes))
    directed_volume = direction * volumes
    directed_volume[0] = volumes[0]
    return np.cumsum(directed_volume)


def atr(highs, lows, closes, period=14):
    """ATR with Wilder RMA smoothing. (QBase volatility/atr.py)"""
    n = len(closes)
    if n == 0 or n < period + 1:
        return np.full(n, np.nan)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hc, lc)
    out = np.full(n, np.nan)
    out[period] = np.mean(tr[1: period + 1])
    alpha_w = 1.0 / period
    for i in range(period + 1, n):
        out[i] = out[i - 1] * (1.0 - alpha_w) + tr[i] * alpha_w
    return out


# ═══════════════════════════════════════════════════════════════
# Section 3: SIGNAL (QBase v9 信号逻辑)
# ═══════════════════════════════════════════════════════════════

def signal_v9(closes, roc_arr, obv_ema_arr, bar_idx):
    """ROC Momentum + OBV EMA Trend. Long only → clamp to [0, 1].

    ROC > 0 AND OBV EMA rising → min(1.0, |ROC|/5)
    else → 0.0
    """
    if bar_idx < 1:
        return 0.0
    roc_val = roc_arr[bar_idx]
    obv_curr = obv_ema_arr[bar_idx]
    obv_prev = obv_ema_arr[bar_idx - 1]

    if np.isnan(roc_val) or np.isnan(obv_curr) or np.isnan(obv_prev):
        return 0.0

    if roc_val <= 0:
        return 0.0

    obv_rising = obv_curr > obv_prev
    if not obv_rising:
        return 0.0

    return min(1.0, abs(roc_val) / 5.0)


# ═══════════════════════════════════════════════════════════════
# Section 4: POSITION SIZING — Vol Targeting + Carver Buffer
# ═══════════════════════════════════════════════════════════════

def calc_optimal_lots(forecast, atr_val, price, capital, max_lots):
    """Vol-targeted continuous lots (fractional)."""
    if price <= 0 or atr_val <= 0 or np.isnan(atr_val) or forecast == 0:
        return 0.0
    realized_vol = (atr_val * math.sqrt(ANNUAL_FACTOR)) / price
    if realized_vol <= 0:
        return 0.0
    vol_scalar = TARGET_VOL / realized_vol
    notional = price * MULTIPLIER
    raw = (forecast / 10.0) * vol_scalar * (capital / notional)
    return max(0.0, min(raw, float(max_lots)))


def apply_buffer(optimal, current):
    """Carver 10% buffer: only trade if optimal outside buffer zone."""
    buffer = max(abs(optimal) * BUFFER_FRACTION, 0.5)
    if (current - buffer) <= optimal <= (current + buffer):
        return current
    if optimal > current + buffer:
        target = math.floor(optimal - buffer)
    else:
        target = math.ceil(optimal + buffer)
    if abs(target - current) < MIN_TRADE_SIZE:
        return current
    return max(0, target)


# ═══════════════════════════════════════════════════════════════
# Section 5: RISK — 移动止损 + 硬止损 + Chandelier + Portfolio Stops
# ═══════════════════════════════════════════════════════════════

def chandelier_exit_triggered(highs, closes, atr_arr, bar_idx):
    """Chandelier Exit: close < highest_high(period) - mult × ATR."""
    if bar_idx < CHANDELIER_PERIOD:
        return False
    atr_val = atr_arr[bar_idx]
    if np.isnan(atr_val):
        return False
    start = bar_idx - CHANDELIER_PERIOD + 1
    highest = np.max(highs[start:bar_idx + 1])
    return bool(closes[bar_idx] < highest - CHANDELIER_MULT * atr_val)


def check_portfolio_stops(equity, peak_equity, daily_start_equity):
    """Returns (action, value). action: circuit/reduce/warning/daily_stop/ok."""
    dd = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
    daily_pnl = (equity - daily_start_equity) / daily_start_equity if daily_start_equity > 0 else 0.0
    if dd <= STOP_CIRCUIT:
        return ("circuit", dd)
    if dd <= STOP_REDUCE:
        return ("reduce", dd)
    if dd <= STOP_WARNING:
        return ("warning", dd)
    if daily_pnl <= STOP_DAILY:
        return ("daily_stop", daily_pnl)
    return ("ok", dd)


# ═══════════════════════════════════════════════════════════════
# Section 6: OPERATIONS — 状态持久化 + 交易日检测
# ═══════════════════════════════════════════════════════════════

def get_trading_day():
    """当前时间+4小时推算交易日 (夜盘自动归下一天)."""
    shifted = datetime.now() + timedelta(hours=4)
    wd = shifted.weekday()
    if wd == 5:
        shifted += timedelta(days=2)
    elif wd == 6:
        shifted += timedelta(days=1)
    return shifted.strftime("%Y%m%d")


def save_state(state_dict, strategy_name="DailyV9_ROC_OBV"):
    """原子写JSON: temp → fsync → rename."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{strategy_name}_state.json")
    tmp = path + ".tmp"
    state_dict["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(tmp, "w") as f:
        json.dump(state_dict, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        bak = path + ".bak"
        try:
            os.replace(path, bak)
        except OSError:
            pass
    os.replace(tmp, path)


def load_state(strategy_name="DailyV9_ROC_OBV"):
    """读主文件, 失败读备份."""
    for suffix in ("", ".bak"):
        path = os.path.join(STATE_DIR, f"{strategy_name}_state.json{suffix}")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    return None


# ═══════════════════════════════════════════════════════════════
# Section 7: FEISHU — 非阻塞飞书通知 (daemon线程)
# ═══════════════════════════════════════════════════════════════

_feishu_queue = queue.Queue(maxsize=200)

_COLORS = {"open": "green", "add": "blue", "reduce": "orange",
           "close": "red", "error": "carmine", "info": "purple",
           "trail_stop": "red", "hard_stop": "carmine", "circuit": "carmine"}
_LABELS = {"open": "开仓", "add": "加仓", "reduce": "减仓",
           "close": "平仓", "error": "异常", "info": "信息",
           "trail_stop": "移动止损", "hard_stop": "硬止损", "circuit": "熔断"}


def _feishu_sign(secret):
    ts = str(int(time.time()))
    hmac_code = hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    return ts, base64.b64encode(hmac_code).decode()


def _feishu_worker():
    while True:
        try:
            payload = _feishu_queue.get(timeout=2)
            if FEISHU_SECRET:
                ts, sign = _feishu_sign(FEISHU_SECRET)
                payload["timestamp"] = ts
                payload["sign"] = sign
            requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=5)
            time.sleep(0.2)
        except queue.Empty:
            continue
        except Exception:
            pass

_feishu_thread = threading.Thread(target=_feishu_worker, daemon=True)
_feishu_thread.start()


def feishu_send(action, symbol, msg):
    """非阻塞发送, 队列满则丢弃."""
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK_URL:
        return
    color = _COLORS.get(action, "grey")
    label = _LABELS.get(action, action)
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text",
                                 "content": f"{label} | {symbol}"},
                       "template": color},
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md",
                                   "content": f"{msg}\n\n---\n*{time.strftime('%Y-%m-%d %H:%M:%S')}*"}}],
        },
    }
    try:
        _feishu_queue.put_nowait(payload)
    except queue.Full:
        pass


# ═══════════════════════════════════════════════════════════════
# Section 8: PYTHONGO STRATEGY
# ═══════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2509", title="合约代码")
    kline_style: str = Field(default="D1", title="K线周期")
    max_position: int = Field(default=10, title="最大持仓手数")
    capital: float = Field(default=1_000_000, title="配置资金")


class State(BaseState):
    forecast: float = Field(default=0.0, title="信号强度")
    target_lots: int = Field(default=0, title="目标手数")
    net_pos: int = Field(default=0, title="当前持仓")
    peak_price: float = Field(default=0.0, title="持仓最高价")
    trading_day: str = Field(default="", title="交易日")
    last_action: str = Field(default="—", title="上次操作")


class DailyV9_ROC_OBV(BaseStrategy):
    """铁矿石日线做多 — ROC Momentum + OBV EMA Trend

    QBase_v2: strong_trend_long_I_daily_v9
    """

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_gen = None

        # Next-bar pending
        self._pending = None          # "OPEN"/"ADD"/"REDUCE"/"CLOSE"/"TRAIL_STOP"/"HARD_STOP"/"CIRCUIT"
        self._pending_target = None   # int

        # 持仓追踪
        self.peak_price = 0.0
        self.entry_equity = 0.0       # 开仓时权益 (硬止损参考)

        # 权益追踪
        self.peak_equity = 0.0
        self.daily_start_equity = 0.0
        self._current_trading_day = ""

        # 委托追踪
        self.order_ids = set()

    @property
    def main_indicator_data(self):
        return {"forecast": self.state_map.forecast}

    # ── Lifecycle ──────────────────────────────────────────────

    def on_start(self):
        p = self.params_map

        self.kline_gen = KLineGenerator(
            callback=self._on_bar_complete,
            real_time_callback=self._on_bar_update,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self.kline_gen.push_history_data()

        # 恢复持久化状态
        saved = load_state()
        if saved:
            self.peak_equity = saved.get("peak_equity", 0.0)
            self.daily_start_equity = saved.get("daily_start_equity", 0.0)
            self.peak_price = saved.get("peak_price", 0.0)
            self._current_trading_day = saved.get("trading_day", "")
            self.output(f"恢复状态: peak_eq={self.peak_equity:.0f} peak_px={self.peak_price:.1f}")

        # 初始化权益
        account = self.get_account_fund_data("")
        if account:
            eq = account.balance
            if self.peak_equity == 0.0:
                self.peak_equity = eq
            if self.daily_start_equity == 0.0:
                self.daily_start_equity = eq

        # 查实际持仓 (重启恢复: 信任broker)
        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual > 0 and self.peak_price == 0.0:
            # 有仓但没有peak_price记录, 用当前价
            self.output(f"重启检测到持仓 {actual}手, peak_price未知")

        super().on_start()
        self.output(
            f"DailyV9 启动 | {p.instrument_id}@{p.exchange} | "
            f"D1 | max={p.max_position} capital={p.capital:,.0f} | 持仓={actual}"
        )
        feishu_send("info", p.instrument_id,
                    f"**策略启动**: DailyV9 ROC+OBV\n**持仓**: {actual}手")

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_gen.tick_to_kline(tick)

        # 交易日切换检测
        td = get_trading_day()
        if td != self._current_trading_day and self._current_trading_day:
            account = self.get_account_fund_data("")
            if account:
                self.daily_start_equity = account.balance
            self._current_trading_day = td
            self.state_map.trading_day = td
            self.output(f"新交易日: {td}, daily_start_equity={self.daily_start_equity:.0f}")

        if not self._current_trading_day:
            self._current_trading_day = td
            self.state_map.trading_day = td

    def on_stop(self):
        self._save_state()
        super().on_stop()
        self.output("策略停止, 状态已保存")

    # ── K线回调 ────────────────────────────────────────────────

    def _on_bar_update(self, kline: KLineData):
        self._push_widget(kline)

    def _on_bar_complete(self, kline: KLineData):
        """Daily K线完成 (15:00) — 主交易逻辑."""
        p = self.params_map
        signal_price = 0.0

        # ── 1. 撤挂单 ──
        for oid in list(self.order_ids):
            self.cancel_order(oid)

        # ── 2. 执行pending (next-bar规则) ──
        if self._pending is not None:
            signal_price = self._execute_pending(kline)

        # ── 3. 数据准备 ──
        producer = self.kline_gen.producer
        closes = np.array(producer.close, dtype=np.float64)
        highs = np.array(producer.high, dtype=np.float64)
        lows = np.array(producer.low, dtype=np.float64)
        volumes = np.array(producer.volume, dtype=np.float64)
        bar_idx = len(closes) - 1

        if bar_idx < WARMUP:
            self._push_widget(kline, signal_price)
            return

        # ── 4. 指标计算 ──
        roc_arr = rate_of_change(closes, ROC_PERIOD)
        obv_arr = obv(closes, volumes)
        obv_ema_arr = ema(obv_arr, OBV_EMA_PERIOD)
        atr_arr = atr(highs, lows, closes, VOL_ATR_PERIOD)

        # ── 5. 信号 ──
        raw = signal_v9(closes, roc_arr, obv_ema_arr, bar_idx)
        forecast = min(FORECAST_CAP, raw * FORECAST_SCALAR)
        forecast = max(0.0, forecast)  # long only
        self.state_map.forecast = forecast

        # ── 6. 仓位计算 ──
        optimal = calc_optimal_lots(forecast, atr_arr[bar_idx], closes[bar_idx],
                                    p.capital, p.max_position)
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        target = apply_buffer(optimal, current)
        self.state_map.net_pos = current

        # ── 7. 止损检查 (优先级: 硬止损 > 移动止损 > Chandelier > Portfolio) ──

        # 硬止损: 浮亏 > 2% 账户权益
        account = self.get_account_fund_data("")
        if account and current > 0:
            equity = account.balance
            position_profit = account.position_profit if hasattr(account, 'position_profit') else 0
            if position_profit < 0 and abs(position_profit) > equity * HARD_STOP_EQUITY_PCT:
                self._pending = "HARD_STOP"
                self._pending_target = 0
                self.output(f"硬止损! 浮亏{position_profit:.0f} > 权益{equity:.0f}×{HARD_STOP_EQUITY_PCT:.0%}")
                self._push_widget(kline, signal_price)
                self.update_status_bar()
                return

        # 移动止损
        close = closes[bar_idx]
        if current > 0:
            if close > self.peak_price:
                self.peak_price = close
            if self.peak_price > 0 and close <= self.peak_price * (1 - TRAILING_PCT / 100):
                self._pending = "TRAIL_STOP"
                self._pending_target = 0
                self.output(f"移动止损! close={close:.1f} < peak={self.peak_price:.1f}×{1-TRAILING_PCT/100:.3f}")
                self._push_widget(kline, signal_price)
                self.update_status_bar()
                return

        # Chandelier Exit
        atr_ch = atr(highs, lows, closes, CHANDELIER_PERIOD)
        if current > 0 and chandelier_exit_triggered(highs, closes, atr_ch, bar_idx):
            self._pending = "CLOSE"
            self._pending_target = 0
            self.output("Chandelier Exit 触发")
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # Portfolio Stops
        if account:
            equity = account.balance
            if equity > self.peak_equity:
                self.peak_equity = equity
            stop_action, val = check_portfolio_stops(equity, self.peak_equity, self.daily_start_equity)
            if stop_action == "circuit":
                self._pending = "CIRCUIT"
                self._pending_target = 0
                self.output(f"熔断! {val:.1%}")
                self._push_widget(kline, signal_price)
                return
            elif stop_action == "reduce":
                target = max(0, target // 2)
                self.output(f"减仓! {val:.1%}")
            elif stop_action == "daily_stop":
                self._pending = "CLOSE"
                self._pending_target = 0
                self.output(f"单日止损! {val:.1%}")
                self._push_widget(kline, signal_price)
                return
            elif stop_action == "warning":
                self.output(f"预警 {val:.1%}")

        # ── 8. 生成pending ──
        if target != current:
            if current == 0 and target > 0:
                self._pending = "OPEN"
            elif target == 0 and current > 0:
                self._pending = "CLOSE"
            elif target > current:
                self._pending = "ADD"
            else:
                self._pending = "REDUCE"
            self._pending_target = target

        self.state_map.target_lots = target
        self.state_map.last_action = self._pending or "HOLD"

        self.output(
            f"[BAR] ROC={roc_arr[bar_idx]:.2f} f={forecast:.1f} "
            f"optimal={optimal:.1f} target={target} current={current} "
            f"pending={self._pending or '—'}"
        )
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ── 执行 ───────────────────────────────────────────────────

    def _execute_pending(self, kline: KLineData) -> float:
        """执行pending信号, 返回signal_price供图表标记."""
        action = self._pending
        target = self._pending_target if self._pending_target is not None else 0
        self._pending = None
        self._pending_target = None

        p = self.params_map
        price = kline.close
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        diff = target - current

        if diff == 0:
            return 0.0

        # 保证金检查
        if diff > 0:
            account = self.get_account_fund_data("")
            if account:
                needed = price * MULTIPLIER * abs(diff) * 0.15  # 保守估算
                if needed > account.available * 0.6:
                    self.output(f"保证金不足! 需要{needed:.0f} > 可用{account.available:.0f}×60%")
                    feishu_send("error", p.instrument_id,
                                f"**保证金不足**\n需要: {needed:,.0f}\n可用: {account.available:,.0f}")
                    return 0.0

        # 下单
        if diff > 0:
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=abs(diff), price=price,
                order_direction="buy", market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)
            if action == "OPEN":
                self.peak_price = price
                account = self.get_account_fund_data("")
                if account:
                    self.entry_equity = account.balance
        else:
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=abs(diff), price=price,
                order_direction="sell", market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)
            if target == 0:
                self.peak_price = 0.0

        # 飞书
        action_lower = action.lower() if action else "info"
        feishu_send(action_lower, p.instrument_id,
                    f"**操作**: {_LABELS.get(action_lower, action)}\n"
                    f"**手数**: {abs(diff)}手 ({'买' if diff > 0 else '卖'})\n"
                    f"**价格**: {price:,.1f}\n"
                    f"**持仓**: {current} → {target}\n"
                    f"**信号**: {self.state_map.forecast:.1f}/20")

        self.output(
            f"[执行{action}] {p.instrument_id} "
            f"{'买' if diff > 0 else '卖'}{abs(diff)}手 @ {price:.1f} "
            f"({current}→{target})"
        )
        self.state_map.last_action = action
        self._save_state()
        return price if diff > 0 else -price

    # ── 辅助 ───────────────────────────────────────────────────

    def _push_widget(self, kline: KLineData, signal_price: float = 0.0):
        try:
            self.widget.recv_kline({
                "kline": kline,
                "signal_price": signal_price,
                **self.main_indicator_data,
            })
        except Exception:
            pass

    def _save_state(self):
        save_state({
            "peak_equity": self.peak_equity,
            "daily_start_equity": self.daily_start_equity,
            "peak_price": self.peak_price,
            "trading_day": self._current_trading_day,
            "net_pos": self.state_map.net_pos,
            "forecast": self.state_map.forecast,
        })

    # ── 回调 ───────────────────────────────────────────────────

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_ids.discard(trade.order_id)
        # 更新持仓显示
        pos = self.get_position(self.params_map.instrument_id)
        self.state_map.net_pos = pos.net_position if pos else 0
        self.output(f"[成交] {trade.direction} {trade.volume}手 @ {trade.price}")
        self._save_state()
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_ids.discard(order.order_id)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu_send("error", self.params_map.instrument_id, f"**异常**: {error}")
