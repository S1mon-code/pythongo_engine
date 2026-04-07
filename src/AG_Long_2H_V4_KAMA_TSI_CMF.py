"""AG_Long_2H_V4 — KAMA + TSI + CMF (白银2H做多)

QBase_v2 策略: strong_trend_long_AG_2h_v4
信号逻辑:
  KAMA(55)上升 AND TSI(35,18) > 0 AND CMF(50) > 0 → 做多信号
  强度 = min(1.0, tsi/25*0.5 + cmf*2.0 + 0.2), clamp [0, 1]
仓位: Vol Targeting 每根K线重算 + Carver 10% buffer
止损: 移动止损 + 2%权益硬止损 + Chandelier Exit + Portfolio Stops

部署: 复制到 Windows 无限易 pyStrategy/self_strategy/
"""
import math
import os
import json
import time
from datetime import datetime, timedelta

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.feishu import feishu, LABELS


# ═══════════════════════════════════════════════════════════════
# Section 1: CONFIG
# ═══════════════════════════════════════════════════════════════

# 合约参数 (白银 AG)
MULTIPLIER = 15
TICK_SIZE = 1

# 策略指标参数 (QBase优化值)
KAMA_PERIOD = 55
TSI_LONG_PERIOD = 35
TSI_SHORT_PERIOD = 18
TSI_SIGNAL_PERIOD = 7
CMF_PERIOD = 50
WARMUP = 80

# Vol Targeting
TARGET_VOL = 0.15
VOL_ATR_PERIOD = 14
ANNUAL_FACTOR = 252 * 5           # 2H bars: AG ~5 bars/day (9:00-15:00 + 21:00-02:30 ≈ 10.5h)

# Forecast (单策略, 无blending)
FORECAST_SCALAR = 10.0            # raw signal [0,1] × 10 → forecast [0,10]
FORECAST_CAP = 20.0

# Carver Buffer
BUFFER_FRACTION = 0.10
MIN_TRADE_SIZE = 1

# 止损
TRAILING_PCT = 2.0                # 移动止损 2%
HARD_STOP_EQUITY_PCT = 0.02       # 账户权益 2% 硬止损
STOP_WARNING = -0.10
STOP_REDUCE = -0.15
STOP_CIRCUIT = -0.20
STOP_DAILY = -0.05

# Chandelier Exit
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 3.0             # v4策略参数

# 换月提醒 (天数)
ROLLOVER_WARN_DAYS = 15
ROLLOVER_URGENT_DAYS = 5

# 状态文件路径
STATE_DIR = "./state"


# ═══════════════════════════════════════════════════════════════
# Section 2: INDICATORS (从QBase原样移植, 纯numpy)
# ═══════════════════════════════════════════════════════════════

def _ema(arr, period):
    """EMA with SMA seed. Returns NaN before period-1. (QBase _utils.py)"""
    result = np.full(arr.size, np.nan)
    if arr.size < period:
        return result
    result[period - 1] = arr[:period].mean()
    k = 2.0 / (period + 1.0)
    for i in range(period, arr.size):
        result[i] = arr[i] * k + result[i - 1] * (1.0 - k)
    return result


def kama(closes, period=55):
    """KAMA: Kaufman Adaptive Moving Average.

    根据效率比率(ER)自适应调整平滑系数:
    - 趋势明显时(ER→1): 快速跟随
    - 震荡时(ER→0): 缓慢响应
    fast_sc = 2/(2+1), slow_sc = 2/(30+1)
    """
    n = len(closes)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    fast_sc = 2.0 / 3.0
    slow_sc = 2.0 / 31.0
    out[period] = closes[period]
    for i in range(period + 1, n):
        direction = abs(closes[i] - closes[i - period])
        volatility = np.sum(np.abs(np.diff(closes[i - period:i + 1])))
        if volatility == 0:
            er = 0.0
        else:
            er = direction / volatility
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        out[i] = out[i - 1] + sc * (closes[i] - out[i - 1])
    return out


def tsi(closes, long_period=35, short_period=18):
    """TSI: True Strength Index — 双重平滑动量指标.

    momentum → EMA(long) → EMA(short)
    |momentum| → EMA(long) → EMA(short)
    TSI = 100 × double_smooth(momentum) / double_smooth(|momentum|)
    信号线 = EMA(TSI, 7)
    """
    n = len(closes)
    if n < 2:
        return np.full(n, np.nan), np.full(n, np.nan)
    momentum = np.zeros(n)
    momentum[1:] = closes[1:] - closes[:-1]
    abs_momentum = np.abs(momentum)
    # 双重EMA平滑
    smooth1 = _ema(momentum, long_period)
    smooth2 = _ema(smooth1, short_period)
    abs_smooth1 = _ema(abs_momentum, long_period)
    abs_smooth2 = _ema(abs_smooth1, short_period)
    tsi_line = np.where(abs_smooth2 != 0, 100.0 * smooth2 / abs_smooth2, 0.0)
    # NaN传播: 如果abs_smooth2是NaN, tsi_line也应该是NaN
    tsi_line = np.where(np.isnan(abs_smooth2), np.nan, tsi_line)
    tsi_signal = _ema(tsi_line, TSI_SIGNAL_PERIOD)
    return tsi_line, tsi_signal


def cmf(highs, lows, closes, volumes, period=50):
    """CMF: Chaikin Money Flow — 资金流量指标.

    MFM = ((close - low) - (high - close)) / (high - low)
    MFV = MFM × volume
    CMF = sum(MFV, period) / sum(volume, period)
    """
    n = len(closes)
    out = np.full(n, np.nan)
    hl_range = highs - lows
    mfm = np.where(hl_range != 0,
                   ((closes - lows) - (highs - closes)) / hl_range,
                   0.0)
    mfv = mfm * volumes
    for i in range(period - 1, n):
        vol_sum = np.sum(volumes[i - period + 1:i + 1])
        if vol_sum == 0:
            out[i] = 0.0
        else:
            out[i] = np.sum(mfv[i - period + 1:i + 1]) / vol_sum
    return out


def atr(highs, lows, closes, period=14):
    """ATR: Average True Range — Wilder RMA平滑."""
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
# Section 3: SIGNAL (QBase v4 信号逻辑 — KAMA + TSI + CMF)
# ═══════════════════════════════════════════════════════════════

def signal_v4(kama_arr, tsi_arr, cmf_arr, bar_idx):
    """KAMA rising + TSI > 0 + CMF > 0. Long only → clamp to [0, 1].

    入场条件: KAMA上升 (k > k_prev) AND TSI > 0 AND CMF > 0
    信号强度: min(1.0, tsi/25*0.5 + cmf*2.0 + 0.2)
    """
    if bar_idx < 1:
        return 0.0

    k_curr = kama_arr[bar_idx]
    k_prev = kama_arr[bar_idx - 1]
    tsi_val = tsi_arr[bar_idx]
    cmf_val = cmf_arr[bar_idx]

    # NaN检查
    if (np.isnan(k_curr) or np.isnan(k_prev) or
            np.isnan(tsi_val) or np.isnan(cmf_val)):
        return 0.0

    # 三重条件: KAMA上升 + TSI > 0 + CMF > 0
    kama_rising = k_curr > k_prev
    if not kama_rising:
        return 0.0
    if tsi_val <= 0:
        return 0.0
    if cmf_val <= 0:
        return 0.0

    # 信号强度
    strength = tsi_val / 25.0 * 0.5 + cmf_val * 2.0 + 0.2
    return max(0.0, min(1.0, strength))


# ═══════════════════════════════════════════════════════════════
# Section 4: POSITION SIZING — Vol Targeting + Carver Buffer
# ═══════════════════════════════════════════════════════════════

def calc_optimal_lots(forecast, atr_val, price, capital, max_lots):
    """Vol-targeted continuous lots (fractional).

    realized_vol = ATR × sqrt(annual_factor) / price
    vol_scalar = target_vol / realized_vol
    raw = (forecast / 10) × vol_scalar × (capital / notional)
    """
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
    """Carver 10% buffer: 只在最优仓位超出buffer区域时才交易."""
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
    """Chandelier Exit: close < highest_high(period) - mult x ATR."""
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
    daily_pnl = ((equity - daily_start_equity) / daily_start_equity
                 if daily_start_equity > 0 else 0.0)
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

STRATEGY_NAME = "AG_Long_2H_V4_KAMA_TSI_CMF"


def get_trading_day():
    """当前时间+4小时推算交易日 (夜盘自动归下一天)."""
    shifted = datetime.now() + timedelta(hours=4)
    wd = shifted.weekday()
    if wd == 5:
        shifted += timedelta(days=2)
    elif wd == 6:
        shifted += timedelta(days=1)
    return shifted.strftime("%Y%m%d")


def save_state(state_dict):
    """原子写JSON: temp -> fsync -> rename."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{STRATEGY_NAME}_state.json")
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


def load_state():
    """读主文件, 失败读备份."""
    for suffix in ("", ".bak"):
        path = os.path.join(STATE_DIR, f"{STRATEGY_NAME}_state.json{suffix}")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    return None


# ═══════════════════════════════════════════════════════════════
# Section 7: PYTHONGO STRATEGY
# ═══════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="SHFE", title="交易所代码")
    instrument_id: str = Field(default="ag2506", title="合约代码")
    kline_style: str = Field(default="H2", title="K线周期")
    max_position: int = Field(default=10, title="最大持仓手数")
    capital: float = Field(default=1_000_000, title="配置资金")


class State(BaseState):
    forecast: float = Field(default=0.0, title="信号强度")
    target_lots: int = Field(default=0, title="目标手数")
    net_pos: int = Field(default=0, title="当前持仓")
    peak_price: float = Field(default=0.0, title="持仓最高价")
    trading_day: str = Field(default="", title="交易日")
    last_action: str = Field(default="—", title="上次操作")


class AG_Long_2H_V4_KAMA_TSI_CMF(BaseStrategy):
    """白银2H做多 — KAMA Adaptive + TSI Momentum + CMF Volume

    QBase_v2: strong_trend_long_AG_2h_v4
    入场: KAMA(55)上升 + TSI(35,18) > 0 + CMF(50) > 0
    """

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_gen = None

        # Next-bar pending (本bar信号 → 下bar执行)
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

        # 账户ID (on_start中初始化)
        self._investor_id = ""

    @property
    def main_indicator_data(self):
        return {"forecast": self.state_map.forecast}

    # ── Lifecycle ──────────────────────────────────────────────

    def on_start(self):
        p = self.params_map

        # 账户ID (不能传空字符串给get_account_fund_data, 必须在push_history_data之前)
        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id

        # 恢复持久化状态 (必须在push_history_data之前, 历史bar回调需要这些值)
        saved = load_state()
        if saved:
            self.peak_equity = saved.get("peak_equity", 0.0)
            self.daily_start_equity = saved.get("daily_start_equity", 0.0)
            self.peak_price = saved.get("peak_price", 0.0)
            self._current_trading_day = saved.get("trading_day", "")
            self.output(
                f"恢复状态: peak_eq={self.peak_equity:.0f} "
                f"peak_px={self.peak_price:.1f}"
            )

        # 初始化权益
        if self._investor_id:
            account = self.get_account_fund_data(self._investor_id)
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
            self.output(f"重启检测到持仓 {actual}手, peak_price未知")

        # 初始化K线生成器 + 推送历史数据 (必须在investor_id和状态恢复之后)
        self.kline_gen = KLineGenerator(
            callback=self._on_bar_complete,
            real_time_callback=self._on_bar_update,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self.kline_gen.push_history_data()

        super().on_start()
        self.output(
            f"AG_V4 启动 | {p.instrument_id}@{p.exchange} | "
            f"H2 | max={p.max_position} capital={p.capital:,.0f} | 持仓={actual}"
        )
        feishu(
            "start", p.instrument_id,
            f"**策略启动**: AG_V4 KAMA+TSI+CMF\n"
            f"**合约**: {p.instrument_id}@{p.exchange}\n"
            f"**周期**: H2 | **持仓**: {actual}手",
        )

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_gen.tick_to_kline(tick)

        # 交易日切换检测
        td = get_trading_day()
        if td != self._current_trading_day and self._current_trading_day:
            account = (self.get_account_fund_data(self._investor_id)
                       if self._investor_id else None)
            if account:
                self.daily_start_equity = account.balance
            self._current_trading_day = td
            self.state_map.trading_day = td
            self.output(
                f"新交易日: {td}, "
                f"daily_start_equity={self.daily_start_equity:.0f}"
            )

        if not self._current_trading_day:
            self._current_trading_day = td
            self.state_map.trading_day = td

    def on_stop(self):
        self._save_state()
        super().on_stop()
        self.output("策略停止, 状态已保存")

    # ── K线回调 ────────────────────────────────────────────────

    def _on_bar_update(self, kline: KLineData):
        """实时K线更新 — 仅推图表."""
        self._push_widget(kline)

    def _on_bar_complete(self, kline: KLineData):
        """2H K线完成 — 主交易逻辑."""
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
        kama_arr = kama(closes, KAMA_PERIOD)
        tsi_arr, _tsi_signal = tsi(closes, TSI_LONG_PERIOD, TSI_SHORT_PERIOD)
        cmf_arr = cmf(highs, lows, closes, volumes, CMF_PERIOD)
        atr_arr = atr(highs, lows, closes, VOL_ATR_PERIOD)

        # ── 5. 信号 ──
        raw = signal_v4(kama_arr, tsi_arr, cmf_arr, bar_idx)
        forecast = min(FORECAST_CAP, raw * FORECAST_SCALAR)
        forecast = max(0.0, forecast)  # long only: clamp to [0, max]
        self.state_map.forecast = forecast

        # ── 6. 仓位计算 ──
        optimal = calc_optimal_lots(
            forecast, atr_arr[bar_idx], closes[bar_idx],
            p.capital, p.max_position,
        )
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        target = apply_buffer(optimal, current)
        self.state_map.net_pos = current

        # ── 7. 止损检查 (优先级: 硬止损 > 移动止损 > Chandelier > Portfolio) ──

        # 硬止损: 浮亏 > 2% 账户权益
        account = (self.get_account_fund_data(self._investor_id)
                   if self._investor_id else None)
        if account and current > 0:
            equity = account.balance
            position_profit = (account.position_profit
                               if hasattr(account, "position_profit") else 0)
            if (position_profit < 0
                    and abs(position_profit) > equity * HARD_STOP_EQUITY_PCT):
                self._pending = "HARD_STOP"
                self._pending_target = 0
                self.output(
                    f"硬止损! 浮亏{position_profit:.0f} > "
                    f"权益{equity:.0f}x{HARD_STOP_EQUITY_PCT:.0%}"
                )
                self._push_widget(kline, signal_price)
                self.update_status_bar()
                return

        # 移动止损
        close = closes[bar_idx]
        if current > 0:
            if close > self.peak_price:
                self.peak_price = close
            trail_threshold = self.peak_price * (1 - TRAILING_PCT / 100)
            if self.peak_price > 0 and close <= trail_threshold:
                self._pending = "TRAIL_STOP"
                self._pending_target = 0
                self.output(
                    f"移动止损! close={close:.1f} < "
                    f"peak={self.peak_price:.1f}x{1 - TRAILING_PCT / 100:.3f}"
                )
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

        # Portfolio Stops (account已在硬止损段获取)
        if account:
            equity = account.balance
            if equity > self.peak_equity:
                self.peak_equity = equity
            stop_action, val = check_portfolio_stops(
                equity, self.peak_equity, self.daily_start_equity,
            )
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
            f"[BAR] KAMA={kama_arr[bar_idx]:.1f} "
            f"TSI={tsi_arr[bar_idx]:.2f} CMF={cmf_arr[bar_idx]:.4f} "
            f"f={forecast:.1f} optimal={optimal:.1f} "
            f"target={target} current={current} "
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

        # 保证金检查 (买入时)
        if diff > 0 and self._investor_id:
            account = self.get_account_fund_data(self._investor_id)
            if account:
                needed = price * MULTIPLIER * abs(diff) * 0.15  # 保守估算
                if needed > account.available * 0.6:
                    self.output(
                        f"保证金不足! 需要{needed:.0f} > "
                        f"可用{account.available:.0f}x60%"
                    )
                    feishu(
                        "error", p.instrument_id,
                        f"**保证金不足**\n"
                        f"需要: {needed:,.0f}\n可用: {account.available:,.0f}",
                    )
                    return 0.0

        # 下单
        if diff > 0:
            # 买入开仓/加仓
            oid = self.send_order(
                exchange=p.exchange,
                instrument_id=p.instrument_id,
                volume=abs(diff),
                price=price,
                order_direction="buy",
                market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)
            if action == "OPEN":
                self.peak_price = price
                account = (self.get_account_fund_data(self._investor_id)
                           if self._investor_id else None)
                if account:
                    self.entry_equity = account.balance
        else:
            # 卖出平仓/减仓
            oid = self.auto_close_position(
                exchange=p.exchange,
                instrument_id=p.instrument_id,
                volume=abs(diff),
                price=price,
                order_direction="sell",
                market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)
            if target == 0:
                self.peak_price = 0.0

        # 飞书通知
        action_lower = action.lower() if action else "info"
        feishu(
            action_lower, p.instrument_id,
            f"**操作**: {LABELS.get(action_lower, action)}\n"
            f"**手数**: {abs(diff)}手 ({'买' if diff > 0 else '卖'})\n"
            f"**价格**: {price:,.1f}\n"
            f"**持仓**: {current} -> {target}\n"
            f"**信号**: {self.state_map.forecast:.1f}/20",
        )

        self.output(
            f"[执行{action}] {p.instrument_id} "
            f"{'买' if diff > 0 else '卖'}{abs(diff)}手 @ {price:.1f} "
            f"({current}->{target})"
        )
        self.state_map.last_action = action
        self._save_state()
        return price if diff > 0 else -price

    # ── 辅助 ───────────────────────────────────────────────────

    def _push_widget(self, kline: KLineData, signal_price: float = 0.0):
        """推送图表数据."""
        try:
            self.widget.recv_kline({
                "kline": kline,
                "signal_price": signal_price,
                **self.main_indicator_data,
            })
        except Exception:
            pass

    def _save_state(self):
        """持久化关键状态."""
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
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
