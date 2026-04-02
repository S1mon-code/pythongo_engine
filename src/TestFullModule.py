"""
================================================================================
  TestFullModule — M1 双均线 + 全部运维模块
================================================================================

  交易: MA3/MA7 开仓/加仓/平仓
  止损: 权益止损 > 硬止损 > 移动止损 > Portfolio Stops > 单日止损
  飞书: 非阻塞daemon线程 (开/加/减/平/止损/启动/停止)
  持久化: JSON原子写, 每笔成交后保存
  重启恢复: 读JSON + 信任broker持仓
  交易日检测: +4小时法, 09:00重置daily P&L
  保证金检查: 下单前检查可用资金
  Carver Buffer: 10% buffer最小调仓阈值
  每日回顾: 08:00推送飞书

================================================================================
"""
import math
import os
import json
import time
import threading
from datetime import datetime, timedelta

import numpy as np
import requests

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

MULTIPLIER = 100
STATE_DIR = "./state"
DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15

# Carver Buffer
BUFFER_FRACTION = 0.10
MIN_TRADE_SIZE = 1

# Vol Targeting
TARGET_VOL = 0.15
VOL_ATR_PERIOD = 14
ANNUAL_FACTOR = 252 * 240  # M1

# Portfolio Stops
STOP_WARNING = -0.10
STOP_REDUCE = -0.15
STOP_CIRCUIT = -0.20
STOP_DAILY = -0.05

# 飞书
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/a6aeb603-3d9f-40b5-a5a9-8f0a9cd3bf71"
FEISHU_ENABLED = True


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def atr(highs, lows, closes, period=14):
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
    alpha = 1.0 / period
    for i in range(period + 1, n):
        out[i] = out[i - 1] * (1.0 - alpha) + tr[i] * alpha
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION SIZING (Carver Buffer)
# ══════════════════════════════════════════════════════════════════════════════

def calc_optimal_lots(forecast, atr_val, price, capital, max_lots):
    if price <= 0 or atr_val <= 0 or np.isnan(atr_val) or forecast == 0:
        return 0.0
    realized_vol = (atr_val * math.sqrt(ANNUAL_FACTOR)) / price
    if realized_vol <= 0:
        return 0.0
    notional = price * MULTIPLIER
    raw = (forecast / 10.0) * (TARGET_VOL / realized_vol) * (capital / notional)
    return max(0.0, min(raw, float(max_lots)))


def apply_buffer(optimal, current):
    buffer = max(abs(optimal) * BUFFER_FRACTION, 0.5)
    if (current - buffer) <= optimal <= (current + buffer):
        return current
    if optimal > current + buffer:
        return max(0, math.floor(optimal - buffer))
    else:
        return max(0, math.ceil(optimal + buffer))


# ══════════════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def save_state(data, name="TestFullModule"):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, f"{name}_state.json")
        tmp = path + ".tmp"
        data["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                os.replace(path, path + ".bak")
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        pass


def load_state(name="TestFullModule"):
    for suffix in ("", ".bak"):
        path = os.path.join(STATE_DIR, f"{name}_state.json{suffix}")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  TRADING DAY
# ══════════════════════════════════════════════════════════════════════════════

def get_trading_day():
    shifted = datetime.now() + timedelta(hours=4)
    wd = shifted.weekday()
    if wd == 5:
        shifted += timedelta(days=2)
    elif wd == 6:
        shifted += timedelta(days=1)
    return shifted.strftime("%Y%m%d")


# ══════════════════════════════════════════════════════════════════════════════
#  FEISHU (非阻塞)
# ══════════════════════════════════════════════════════════════════════════════

_COLORS = {"open": "green", "add": "blue", "reduce": "orange",
           "close": "red", "hard_stop": "carmine", "trail_stop": "red",
           "equity_stop": "carmine", "circuit": "carmine", "daily_stop": "carmine",
           "error": "carmine", "start": "turquoise", "shutdown": "grey",
           "daily_review": "purple", "warning": "yellow"}
_LABELS = {"open": "开仓", "add": "加仓", "reduce": "减仓",
           "close": "平仓", "hard_stop": "硬止损", "trail_stop": "移动止损",
           "equity_stop": "权益止损", "circuit": "熔断", "daily_stop": "单日止损",
           "error": "异常", "start": "策略启动", "shutdown": "策略停止",
           "daily_review": "每日回顾", "warning": "预警"}


def _feishu_post(action, symbol, msg):
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
        requests.post(FEISHU_WEBHOOK, json=payload, timeout=3)
    except Exception:
        pass


def feishu(action, symbol, msg):
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK:
        return
    threading.Thread(target=_feishu_post, args=(action, symbol, msg), daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2509", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")
    fast_period: int = Field(default=3, title="快线周期", ge=2)
    slow_period: int = Field(default=7, title="慢线周期", ge=2)
    unit_volume: int = Field(default=1, title="每次手数", ge=1)
    max_lots: int = Field(default=3, title="最大持仓", ge=1)
    capital: float = Field(default=1_000_000, title="配置资金")
    hard_stop_pct: float = Field(default=0.5, title="硬止损-价格(%)")
    trailing_pct: float = Field(default=0.3, title="移动止损(%)")
    equity_stop_pct: float = Field(default=2.0, title="硬止损-权益(%)")


class State(BaseState):
    fast_ma: float = Field(default=0.0, title="快均线")
    slow_ma: float = Field(default=0.0, title="慢均线")
    net_pos: int = Field(default=0, title="净持仓")
    avg_price: float = Field(default=0.0, title="均价")
    peak_price: float = Field(default=0.0, title="最高价")
    hard_stop_line: float = Field(default=0.0, title="价格止损线")
    trail_stop_line: float = Field(default=0.0, title="移动止损线")
    equity: float = Field(default=0.0, title="权益")
    peak_equity: float = Field(default=0.0, title="峰值权益")
    drawdown: str = Field(default="0.00%", title="回撤")
    daily_pnl: str = Field(default="0.00%", title="当日盈亏")
    trading_day: str = Field(default="", title="交易日")
    pending: str = Field(default="—", title="待执行")
    last_action: str = Field(default="—", title="上次操作")
    state_saved: str = Field(default="—", title="上次保存")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

class TestFullModule(BaseStrategy):
    """M1 双均线 — 全模块集成"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        self.avg_price = 0.0
        self.peak_price = 0.0
        self._pending = None
        self.order_id = set()

        # 账户
        self._investor_id = ""
        self._peak_equity = 0.0
        self._daily_start_eq = 0.0
        self._current_trading_day = ""

        # 每日回顾
        self._daily_review_sent = False
        self._today_trades = []

    @property
    def main_indicator_data(self):
        return {
            f"MA{self.params_map.fast_period}": self.state_map.fast_ma,
            f"MA{self.params_map.slow_period}": self.state_map.slow_ma,
        }

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    # ══════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════

    def on_start(self):
        p = self.params_map

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self.kline_generator.push_history_data()

        # 账户ID
        investor = self.get_investor_data(1)
        if investor:
            self._investor_id = investor.investor_id

        # 恢复持久化状态
        saved = load_state()
        if saved:
            self._peak_equity = saved.get("peak_equity", 0.0)
            self._daily_start_eq = saved.get("daily_start_eq", 0.0)
            self.peak_price = saved.get("peak_price", 0.0)
            self.avg_price = saved.get("avg_price", 0.0)
            self._current_trading_day = saved.get("trading_day", "")
            self._today_trades = saved.get("today_trades", [])
            self.output(
                f"[恢复] peak_eq={self._peak_equity:.0f} avg={self.avg_price:.1f} "
                f"peak_px={self.peak_price:.1f} td={self._current_trading_day}"
            )
        else:
            self.output("[恢复] 无保存状态, 首次启动")

        # 初始化权益
        account = self._get_account()
        if account:
            if self._peak_equity == 0.0:
                self._peak_equity = account.balance
            if self._daily_start_eq == 0.0:
                self._daily_start_eq = account.balance

        # 重启恢复: 信任broker
        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0

        # 交易日
        if not self._current_trading_day:
            self._current_trading_day = get_trading_day()
        self.state_map.trading_day = self._current_trading_day

        super().on_start()
        self.output(
            f"TestFullModule 启动 | {p.instrument_id} M1 | 持仓={actual} | "
            f"investor={self._investor_id}"
        )
        feishu("start", p.instrument_id,
               f"**策略**: TestFullModule M1双均线\n"
               f"**合约**: {p.instrument_id}@{p.exchange}\n"
               f"**持仓**: {actual}手\n"
               f"**参数**: MA{p.fast_period}/{p.slow_period} max={p.max_lots}\n"
               f"**止损**: 价格{p.hard_stop_pct}% 移动{p.trailing_pct}% 权益{p.equity_stop_pct}%")

    def on_stop(self):
        self._save_state()
        feishu("shutdown", self.params_map.instrument_id,
               f"**策略停止**\n**持仓**: {self.state_map.net_pos}手")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick — 交易日检测 + 每日回顾
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)

        # 交易日切换
        td = get_trading_day()
        if td != self._current_trading_day and self._current_trading_day:
            old = self._current_trading_day
            # 保存昨日交易记录用于回顾
            self._yesterday_trades = list(self._today_trades)
            self._yesterday_start_eq = self._daily_start_eq
            account = self._get_account()
            self._yesterday_end_eq = account.balance if account else 0

            # 重置
            self._today_trades = []
            if account:
                self._daily_start_eq = account.balance
            self._current_trading_day = td
            self.state_map.trading_day = td
            self._daily_review_sent = False
            self._save_state()
            self.output(f"[交易日切换] {old} → {td}")

        if not self._current_trading_day:
            self._current_trading_day = td
            self.state_map.trading_day = td

        # 每日回顾
        now = datetime.now()
        if (not self._daily_review_sent
                and now.hour == DAILY_REVIEW_HOUR
                and DAILY_REVIEW_MINUTE <= now.minute < DAILY_REVIEW_MINUTE + 5):
            self._send_daily_review()
            self._daily_review_sent = True

    # ══════════════════════════════════════════════════════════════════════
    #  K线回调
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        signal_price = 0.0
        p = self.params_map

        # ── 1. 撤挂单 ──
        for oid in list(self.order_id):
            self.cancel_order(oid)

        # ── 2. 执行pending (执行后return) ──
        if self._pending:
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # ── 3. 指标 ──
        producer = self.kline_generator.producer
        if len(producer.close) < p.slow_period + 2:
            self._push_widget(kline, signal_price)
            return

        close = float(producer.close[-1])
        fast_ma = float(producer.sma(p.fast_period))
        slow_ma = float(producer.sma(p.slow_period))
        self.state_map.fast_ma = round(fast_ma, 2)
        self.state_map.slow_ma = round(slow_ma, 2)

        # ── 4. 持仓 ──
        net_pos = self.get_position(p.instrument_id).net_position
        self.state_map.net_pos = net_pos

        if net_pos == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0
            self.state_map.hard_stop_line = 0.0
            self.state_map.trail_stop_line = 0.0
        else:
            if close > self.peak_price:
                self.peak_price = close
            self.state_map.hard_stop_line = round(self.avg_price * (1 - p.hard_stop_pct / 100), 1)
            self.state_map.trail_stop_line = round(self.peak_price * (1 - p.trailing_pct / 100), 1)

        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.peak_price = round(self.peak_price, 1)

        # ── 5. 权益 ──
        account = self._get_account()
        equity = 0.0
        pos_profit = 0.0
        available = 0.0
        if account:
            equity = account.balance
            pos_profit = account.position_profit
            available = account.available
            if equity > self._peak_equity:
                self._peak_equity = equity
            self.state_map.equity = round(equity, 0)
            self.state_map.peak_equity = round(self._peak_equity, 0)
            dd = (equity - self._peak_equity) / self._peak_equity if self._peak_equity > 0 else 0
            daily_pnl = (equity - self._daily_start_eq) / self._daily_start_eq if self._daily_start_eq > 0 else 0
            self.state_map.drawdown = f"{dd:.2%}"
            self.state_map.daily_pnl = f"{daily_pnl:+.2%}"

        # ══════════════════════════════════════════════════════════
        # 6. 止损 (优先级: 权益 > 硬止损 > 移动 > Portfolio > 正常)
        # ══════════════════════════════════════════════════════════

        # ① 权益止损
        if (net_pos > 0 and account and pos_profit < 0
                and abs(pos_profit) > equity * (p.equity_stop_pct / 100)):
            self._pending = "EQUITY_STOP"
            self.output(f"[权益止损] 浮亏={pos_profit:.0f} > {equity:.0f}×{p.equity_stop_pct}%")

        # ② 硬止损
        elif (net_pos > 0 and self.avg_price > 0
              and close <= self.avg_price * (1 - p.hard_stop_pct / 100)):
            self._pending = "HARD_STOP"
            self.output(f"[硬止损] close={close:.1f} <= {self.state_map.hard_stop_line}")

        # ③ 移动止损
        elif (net_pos > 0 and self.peak_price > 0
              and close <= self.peak_price * (1 - p.trailing_pct / 100)):
            self._pending = "TRAIL_STOP"
            self.output(f"[移动止损] close={close:.1f} <= {self.state_map.trail_stop_line}")

        # ④ Portfolio Stops
        elif net_pos > 0 and account and self._peak_equity > 0:
            dd = (equity - self._peak_equity) / self._peak_equity
            daily_pnl = (equity - self._daily_start_eq) / self._daily_start_eq if self._daily_start_eq > 0 else 0
            if dd <= STOP_CIRCUIT:
                self._pending = "CIRCUIT"
                self.output(f"[熔断] 回撤{dd:.1%}")
            elif dd <= STOP_REDUCE:
                self._pending = "REDUCE"
                self.output(f"[减仓] 回撤{dd:.1%}")
            elif dd <= STOP_WARNING:
                self.output(f"[预警] 回撤{dd:.1%}")
                feishu("warning", p.instrument_id, f"**回撤预警**: {dd:.1%}")
            elif daily_pnl <= STOP_DAILY:
                self._pending = "DAILY_STOP"
                self.output(f"[单日止损] {daily_pnl:.1%}")

        # ── 7. 正常信号 (Carver Buffer) ──
        if self._pending is None:
            # 信号强度
            spread = (fast_ma - slow_ma) / slow_ma if slow_ma > 0 else 0
            raw_signal = min(1.0, max(0.0, spread * 100)) if fast_ma > slow_ma else 0.0
            forecast = raw_signal * 10.0

            # Vol Targeting + Buffer
            highs_arr = np.array(producer.high, dtype=np.float64)
            lows_arr = np.array(producer.low, dtype=np.float64)
            closes_arr = np.array(producer.close, dtype=np.float64)
            atr_arr = atr(highs_arr, lows_arr, closes_arr, VOL_ATR_PERIOD)
            atr_val = atr_arr[-1] if len(atr_arr) > 0 else 0

            optimal = calc_optimal_lots(forecast, atr_val, close, p.capital, p.max_lots)
            target = apply_buffer(optimal, net_pos)

            if target != net_pos:
                if net_pos == 0 and target > 0:
                    self._pending = "OPEN"
                elif target == 0 and net_pos > 0:
                    self._pending = "CLOSE"
                elif target > net_pos:
                    self._pending = "ADD"
                else:
                    self._pending = "REDUCE_SIGNAL"

                # 保存target供执行时用
                self._pending_target = target

        self.state_map.pending = self._pending or "—"
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    # ══════════════════════════════════════════════════════════════════════
    #  执行
    # ══════════════════════════════════════════════════════════════════════

    def _execute(self, kline: KLineData, action: str) -> float:
        price = kline.close
        p = self.params_map
        actual_pos = self.get_position(p.instrument_id).net_position

        # ── 开仓 ──
        if action == "OPEN":
            target = getattr(self, '_pending_target', p.unit_volume)
            vol = max(1, target)

            # 保证金检查
            account = self._get_account()
            if account:
                needed = price * MULTIPLIER * vol * 0.15
                if needed > account.available * 0.6:
                    self.output(f"[保证金不足] 需{needed:.0f} > 可用{account.available:.0f}×60%")
                    feishu("error", p.instrument_id, f"**保证金不足**: 需{needed:,.0f} 可用{account.available:,.0f}")
                    return 0.0

            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price,
                order_direction="buy", market=True,
            )
            if oid is not None:
                self.order_id.add(oid)
            self.avg_price = price
            self.peak_price = price
            self.state_map.last_action = f"建仓{vol}手@{price:.1f}"
            self.output(f"[执行] 建仓 {vol}手 @ {price:.1f}")
            self._record_trade("建仓", vol, "买", price, actual_pos, actual_pos + vol)
            feishu("open", p.instrument_id,
                   f"**建仓** {vol}手 @ {price:,.1f}\n持仓: {actual_pos}→{actual_pos+vol}\n"
                   f"止损线={price*(1-p.hard_stop_pct/100):.1f} 移损线={price*(1-p.trailing_pct/100):.1f}")
            self._save_state()
            return price

        # ── 加仓 ──
        elif action == "ADD":
            target = getattr(self, '_pending_target', actual_pos + p.unit_volume)
            vol = max(1, target - actual_pos)

            account = self._get_account()
            if account:
                needed = price * MULTIPLIER * vol * 0.15
                if needed > account.available * 0.6:
                    self.output(f"[保证金不足] 放弃加仓")
                    return 0.0

            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price,
                order_direction="buy", market=True,
            )
            if oid is not None:
                self.order_id.add(oid)
            if actual_pos > 0:
                self.avg_price = (self.avg_price * actual_pos + price * vol) / (actual_pos + vol)
            else:
                self.avg_price = price
            self.state_map.last_action = f"加仓{vol}手@{price:.1f}"
            self.output(f"[执行] 加仓 {vol}手 @ {price:.1f} | 均价={self.avg_price:.1f}")
            self._record_trade("加仓", vol, "买", price, actual_pos, actual_pos + vol)
            feishu("add", p.instrument_id,
                   f"**加仓** {vol}手 @ {price:,.1f}\n持仓: {actual_pos}→{actual_pos+vol}\n均价: {self.avg_price:.1f}")
            self._save_state()
            return price

        # ── 信号减仓 (Carver Buffer) ──
        elif action == "REDUCE_SIGNAL":
            target = getattr(self, '_pending_target', max(0, actual_pos - 1))
            vol = actual_pos - target
            if vol <= 0 or actual_pos <= 0:
                return 0.0
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price,
                order_direction="sell", market=True,
            )
            if oid is not None:
                self.order_id.add(oid)
            pnl = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
            self.state_map.last_action = f"减仓{vol}手 {pnl:+.2f}%"
            self.output(f"[执行] 减仓 {vol}手 @ {price:.1f} | 剩余{actual_pos-vol}手")
            self._record_trade("减仓", vol, "卖", price, actual_pos, actual_pos - vol)
            feishu("reduce", p.instrument_id,
                   f"**减仓** {vol}手 @ {price:,.1f}\n持仓: {actual_pos}→{actual_pos-vol}\n盈亏: {pnl:+.2f}%")
            self._save_state()
            return -price

        # ── 回撤减仓 (Portfolio -15%) ──
        elif action == "REDUCE":
            vol = max(1, actual_pos // 2)
            if actual_pos <= 0:
                return 0.0
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price,
                order_direction="sell", market=True,
            )
            if oid is not None:
                self.order_id.add(oid)
            self.state_map.last_action = f"回撤减仓{vol}手"
            self.output(f"[执行] ★回撤减仓★ {vol}手 @ {price:.1f}")
            self._record_trade("回撤减仓", vol, "卖", price, actual_pos, actual_pos - vol)
            feishu("reduce", p.instrument_id,
                   f"**★回撤减仓★** {vol}手 @ {price:,.1f}\n持仓: {actual_pos}→{actual_pos-vol}")
            self._save_state()
            return -price

        # ── 全平 ──
        elif action in ("CLOSE", "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP", "CIRCUIT", "DAILY_STOP"):
            labels = {
                "CLOSE": "趋势出场", "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损",
                "EQUITY_STOP": "权益止损", "CIRCUIT": "熔断", "DAILY_STOP": "单日止损",
            }
            label = labels.get(action, action)
            if actual_pos <= 0:
                return 0.0
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=actual_pos, price=price,
                order_direction="sell", market=True,
            )
            if oid is not None:
                self.order_id.add(oid)
            pnl = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
            self.state_map.last_action = f"★{label}★ {pnl:+.2f}%"
            self.output(f"[执行] ★{label}★ {actual_pos}手 @ {price:.1f} | 盈亏={pnl:+.2f}%")
            self._record_trade(label, actual_pos, "卖", price, actual_pos, 0)
            feishu(action.lower(), p.instrument_id,
                   f"**★{label}★** {actual_pos}手 @ {price:,.1f}\n"
                   f"均价: {self.avg_price:.1f}\n盈亏: {pnl:+.2f}%\n"
                   f"Peak: {self.peak_price:.1f}")
            self.avg_price = 0.0
            self.peak_price = 0.0
            self._save_state()
            return -price

        return 0.0

    # ══════════════════════════════════════════════════════════════════════
    #  每日回顾
    # ══════════════════════════════════════════════════════════════════════

    def _send_daily_review(self):
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        net = pos.net_position if pos else 0
        account = self._get_account()
        equity = account.balance if account else 0

        yesterday_start = getattr(self, '_yesterday_start_eq', self._daily_start_eq)
        yesterday_end = getattr(self, '_yesterday_end_eq', equity)
        yesterday_pnl = yesterday_end - yesterday_start
        yesterday_pnl_pct = (yesterday_pnl / yesterday_start * 100) if yesterday_start > 0 else 0
        dd = ((equity - self._peak_equity) / self._peak_equity * 100) if self._peak_equity > 0 else 0

        trades = getattr(self, '_yesterday_trades', self._today_trades)
        trade_lines = ""
        if trades:
            trade_lines = "| 时间 | 操作 | 手数 | 价格 | 持仓 |\n|------|------|------|------|------|\n"
            for t in trades[-10:]:
                trade_lines += f"| {t['time']} | {t['action']} | {t['lots']}手({t['side']}) | {t['price']} | {t['before']}→{t['after']} |\n"
        else:
            trade_lines = "昨日无交易"

        msg = (
            f"**📊 昨日交易回顾**\n\n"
            f"**账户**: 权益={equity:,.0f} 峰值={self._peak_equity:,.0f}\n"
            f"**昨日盈亏**: {yesterday_pnl:+,.0f} ({yesterday_pnl_pct:+.2f}%)\n"
            f"**回撤**: {dd:.2f}%\n"
            f"**持仓**: {net}手 均价={self.avg_price:.1f}\n\n"
            f"**昨日操作**:\n{trade_lines}"
        )
        feishu("daily_review", p.instrument_id, msg)
        self.output("[每日回顾] 已推送飞书")

    # ══════════════════════════════════════════════════════════════════════
    #  辅助
    # ══════════════════════════════════════════════════════════════════════

    def _record_trade(self, action, lots, side, price, before, after):
        self._today_trades.append({
            "time": time.strftime("%H:%M:%S"),
            "action": action, "lots": lots, "side": side,
            "price": round(price, 1), "before": before, "after": after,
        })

    def _save_state(self):
        save_state({
            "peak_equity": self._peak_equity,
            "daily_start_eq": self._daily_start_eq,
            "peak_price": self.peak_price,
            "avg_price": self.avg_price,
            "trading_day": self._current_trading_day,
            "net_pos": self.state_map.net_pos,
            "today_trades": self._today_trades[-50:],
        })
        self.state_map.state_saved = time.strftime("%H:%M:%S")

    def _push_widget(self, kline: KLineData, signal_price: float = 0.0):
        try:
            self.widget.recv_kline({
                "kline": kline,
                "signal_price": signal_price,
                **self.main_indicator_data,
            })
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════
    #  回调
    # ══════════════════════════════════════════════════════════════════════

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_id.discard(trade.order_id)
        self.state_map.net_pos = self.get_position(
            self.params_map.instrument_id
        ).net_position
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
