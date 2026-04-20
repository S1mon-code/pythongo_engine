"""I_Short_Portfolio_V26_V29_TEST — 测试版: 极低门槛频繁进场

⚠️  仅用于测试！信号几乎每根bar都触发，不可用于实盘。
原版: I_Short_Portfolio_V26_V29.py
改动: V26/V29 信号函数门槛极低，warmup缩短，几乎每bar都出信号。
"""
import math
import time
from datetime import datetime

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

# ── 模块导入 ──
from modules.contract_info import get_multiplier, get_tick_size
from modules.error_handler import throttle_on_error
from modules.session_guard import SessionGuard
from modules.feishu import feishu
from modules.persistence import save_state, load_state
from modules.trading_day import get_trading_day, DAY_START_HOUR
from modules.risk import RiskManager
from modules.slippage import SlippageTracker
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.rollover import check_rollover
from modules.position_sizing import calc_optimal_lots, apply_buffer


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "I_Short_Portfolio_V26_V29_TEST"

# ── V26 (H1) 参数 ──
V26_OI_PERIOD = 20
V26_MACD_FAST = 12
V26_MACD_SLOW = 26
V26_MACD_SIGNAL_PERIOD = 9
V26_FLOW_THRESHOLD = 0.2
V26_WARMUP = 0           # ← 测试: 无warmup，依赖push_history_data()历史数据

# ── V29 (H4) 参数 ──
V29_MFI_PERIOD = 20
V29_RSI_PERIOD = 20
V29_EMA_PERIOD = 60
V29_MFI_THRESHOLD = 65
V29_WARMUP = 0           # ← 测试: 无warmup，依赖push_history_data()历史数据

# Chandelier Exit (Short, uses H4 data)
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 3.0

# Vol Targeting (use H4 annual factor since H4 drives decisions)
FORECAST_SCALAR = 10.0
FORECAST_CAP = 20.0
ANNUAL_FACTOR = 252 * 3          # H4: 铁矿石 ~3 bars/day

# 日报时间
DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS (inline, 纯numpy, 从QBase移植)
# ══════════════════════════════════════════════════════════════════════════════

def _ema(arr, period):
    """EMA with SMA seed."""
    n = len(arr)
    out = np.full(n, np.nan)
    if n < period:
        return out
    out[period - 1] = np.mean(arr[:period])
    k = 2.0 / (period + 1)
    for i in range(period, n):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def _macd(closes, fast=12, slow=26, sig_period=9):
    """MACD — returns (line, signal, histogram)."""
    n = len(closes)
    ema_f = _ema(closes, fast)
    ema_s = _ema(closes, slow)
    line = ema_f - ema_s
    sig = np.full(n, np.nan)
    first = -1
    for i in range(n):
        if not np.isnan(line[i]):
            first = i
            break
    if first >= 0:
        sig[first:] = _ema(line[first:], sig_period)
    hist = line - sig
    return line, sig, hist


def _oi_flow(closes, oi, volumes, period=20):
    """OI Flow — open interest change normalized by volume."""
    n = len(closes)
    flow = np.full(n, np.nan)
    if n < 2:
        return flow, np.full(n, np.nan)
    for i in range(1, n):
        if volumes[i] > 0:
            flow[i] = (oi[i] - oi[i - 1]) / (volumes[i] + 1e-10)
        else:
            flow[i] = 0.0
    flow_sig = _ema(flow, period)
    return flow, flow_sig


def _rsi(closes, period=14):
    """RSI — Wilder smoothing."""
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        out[period] = 100.0
    else:
        out[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            out[i + 1] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _mfi(highs, lows, closes, volumes, period=14):
    """MFI — Money Flow Index."""
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tp = (highs + lows + closes) / 3.0
    raw_mf = tp * volumes
    for i in range(period, n):
        pos_flow = neg_flow = 0.0
        for j in range(i - period + 1, i + 1):
            if j > 0 and tp[j] > tp[j - 1]:
                pos_flow += raw_mf[j]
            elif j > 0 and tp[j] < tp[j - 1]:
                neg_flow += raw_mf[j]
        if neg_flow == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1 + pos_flow / neg_flow)
    return out


def _atr(highs, lows, closes, period=14):
    """ATR — Wilder RMA."""
    n = len(closes)
    if n < period + 1:
        return np.full(n, np.nan)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    out = np.full(n, np.nan)
    out[period] = np.mean(tr[1:period + 1])
    a = 1.0 / period
    for i in range(period + 1, n):
        out[i] = out[i - 1] * (1 - a) + tr[i] * a
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL — V26 测试版: 几乎每bar都出信号
# ══════════════════════════════════════════════════════════════════════════════

def generate_signal_v26(closes, oi, volumes, bar_idx):
    """V26 测试版 → 只要过了warmup就出 -0.9 信号."""
    if bar_idx < V26_WARMUP:
        return 0.0
    # 测试: 无条件出强信号
    return -0.9


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL — V29 测试版: 几乎每bar都出信号
# ══════════════════════════════════════════════════════════════════════════════

def generate_signal_v29(closes, highs, lows, volumes, bar_idx):
    """V29 测试版 → 只要过了warmup就出 -0.9 信号."""
    if bar_idx < V29_WARMUP:
        return 0.0
    # 测试: 无条件出强信号
    return -0.9


# ══════════════════════════════════════════════════════════════════════════════
#  CHANDELIER EXIT (SHORT, uses H4 data)
# ══════════════════════════════════════════════════════════════════════════════

def chandelier_short(lows, closes, atr_arr, bar_idx):
    """Short Chandelier: close > lowest_low(period) + mult x ATR."""
    if bar_idx < CHANDELIER_PERIOD:
        return False
    a = atr_arr[bar_idx]
    if np.isnan(a):
        return False
    ll = np.min(lows[bar_idx - CHANDELIER_PERIOD + 1:bar_idx + 1])
    return bool(closes[bar_idx] > ll + CHANDELIER_MULT * a)


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所")
    instrument_id: str = Field(default="i2609", title="合约")
    max_position: int = Field(default=10, title="最大手数")
    capital: float = Field(default=1_000_000, title="资金")
    hard_stop_pct: float = Field(default=0.5, title="硬止损%")
    trailing_pct: float = Field(default=0.3, title="移动止损%")
    equity_stop_pct: float = Field(default=2.0, title="权益止损%")
    flatten_minutes: int = Field(default=5, title="即将收盘提示(分钟)")
    sim_24h: bool = Field(default=True, title="模拟盘")


class State(BaseState):
    signal_v26: float = Field(default=0.0, title="V26信号")
    signal_v29: float = Field(default=0.0, title="V29信号")
    forecast: float = Field(default=0.0, title="预测")
    target_lots: int = Field(default=0, title="目标手")
    net_pos: int = Field(default=0, title="持仓")
    avg_price: float = Field(default=0.0, title="均价")
    trough_price: float = Field(default=0.0, title="谷价")
    trading_day: str = Field(default="", title="交易日")
    equity: float = Field(default=0.0, title="权益")
    drawdown: str = Field(default="---", title="回撤")
    daily_pnl: str = Field(default="---", title="日PnL")
    session: str = Field(default="---", title="时段")
    pending: str = Field(default="---", title="待执行")
    last_action: str = Field(default="---", title="操作")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

class I_Short_Portfolio_V26_V29_TEST(BaseStrategy):
    """⚠️ 测试版 — 铁矿石做空组合, 极低门槛频繁进场"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()

        # Dual timeframe generators
        self.kline_gen_h1 = None
        self.kline_gen_h4 = None

        # Sub-strategy signals
        self._signal_v26 = 0.0
        self._signal_v29 = 0.0

        # OI data for H1 (V26 needs OI)
        self._oi_data_h1 = []

        # Next-bar pending
        self._pending = None
        self._pending_target = None
        self._pending_reason = ""
        self.order_ids = set()

        # 持仓
        self.avg_price = 0.0
        self.trough_price = 0.0

        # 权益
        self._investor_id = ""
        self._risk = None
        self._current_td = ""
        self._daily_review_sent = False
        self._rollover_checked = False
        self._today_trades = []

        # 模块
        self._guard = None
        self._slip = None
        self._hb = None
        self._om = OrderMonitor()
        self._perf = None
        self._multiplier = 100

    @property
    def main_indicator_data(self):
        return {"forecast": self.state_map.forecast}

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    # ══════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════

    def on_start(self):
        p = self.params_map
        self._multiplier = get_multiplier(p.instrument_id)
        self._guard = SessionGuard(p.instrument_id, p.flatten_minutes, sim_24h=p.sim_24h)
        self._slip = SlippageTracker(p.instrument_id)
        self._hb = HeartbeatMonitor(p.instrument_id)
        self._perf = PerformanceTracker(p.instrument_id)

        # H1 generator for V26
        self.kline_gen_h1 = KLineGenerator(
            callback=self._on_bar_h1_complete,
            real_time_callback=self._on_bar_h1_update,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style="H1",
        )
        self.kline_gen_h1.push_history_data()

        # H4 generator for V29
        self.kline_gen_h4 = KLineGenerator(
            callback=self._on_bar_h4_complete,
            real_time_callback=self._on_bar_h4_update,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style="H4",
        )
        self.kline_gen_h4.push_history_data()

        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id

        self._risk = RiskManager(capital=p.capital)

        saved = load_state(STRATEGY_NAME)
        if saved:
            self._risk.load_state(saved)
            self.trough_price = saved.get("trough_price", 0.0)
            self.avg_price = saved.get("avg_price", 0.0)
            self._signal_v26 = saved.get("signal_v26", 0.0)
            self._signal_v29 = saved.get("signal_v29", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._today_trades = saved.get("today_trades", [])

        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance, acct.position_profit)

        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        level, days = check_rollover(p.instrument_id)
        if level:
            feishu("rollover", p.instrument_id, f"**换月**: 距交割月{days}天")

        super().on_start()
        self.output(f"⚠️ TEST {STRATEGY_NAME} 启动 | {p.instrument_id} H1+H4 | 持仓={actual}")
        feishu("start", p.instrument_id,
               f"**⚠️ 测试策略启动** {STRATEGY_NAME}\n合约: {p.instrument_id}\n持仓: {actual}手")

    def on_stop(self):
        self._save()
        feishu("shutdown", self.params_map.instrument_id,
               f"**停止** {STRATEGY_NAME}\n持仓: {self.state_map.net_pos}手")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick — feed BOTH generators
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_gen_h1.tick_to_kline(tick)
        self.kline_gen_h4.tick_to_kline(tick)
        p = self.params_map
        try:
            td = get_trading_day()
            if td != self._current_td and self._current_td:
                acct = self._get_account()
                if acct:
                    self._risk.on_day_change(acct.balance, acct.position_profit)
                self._perf.on_day_change()
                self._today_trades = []
                self._current_td = td
                self.state_map.trading_day = td
                self._daily_review_sent = False
                self._rollover_checked = False
                self._save()

            if not self._current_td:
                self._current_td = td
                self.state_map.trading_day = td

            if not self._rollover_checked:
                level, days = check_rollover(p.instrument_id)
                if level:
                    feishu("rollover", p.instrument_id, f"**换月**: 距交割月{days}天")
                self._rollover_checked = True

            for atype, msg in self._hb.check(p.instrument_id):
                if atype == "no_tick":
                    feishu("no_tick", p.instrument_id, msg)

            self.state_map.session = self._guard.get_status()

            now = datetime.now()
            if (not self._daily_review_sent
                    and now.hour == DAILY_REVIEW_HOUR
                    and DAILY_REVIEW_MINUTE <= now.minute < DAILY_REVIEW_MINUTE + 5):
                self._send_review()
                self._daily_review_sent = True
        except Exception as e:
            self.output(f"[on_tick异常] {type(e).__name__}: {e}")

    # ══════════════════════════════════════════════════════════════════════
    #  H1 K线回调 (V26 — updates signal only, does NOT trigger trades)
    # ══════════════════════════════════════════════════════════════════════

    def _on_bar_h1_update(self, kline: KLineData):
        self._push_widget(kline)

    def _on_bar_h1_complete(self, kline: KLineData):
        try:
            self._on_bar_h1(kline)
        except Exception as e:
            self.output(f"[H1异常] {type(e).__name__}: {e}")

    def _on_bar_h1(self, kline: KLineData):
        """H1 callback: compute V26 signal, store it. No trade decisions."""
        # Collect OI data for H1
        self._oi_data_h1.append(kline.open_interest)

        if not self.trading:
            return

        producer = self.kline_gen_h1.producer
        closes = np.array(producer.close, dtype=np.float64)
        volumes = np.array(producer.volume, dtype=np.float64)
        oi = np.array(self._oi_data_h1, dtype=np.float64)
        bar_idx = len(closes) - 1

        # OI对齐: push_history_data可能多推几根bar
        if len(oi) < V26_WARMUP:
            self._push_widget(kline)
            return
        if len(oi) < len(closes):
            offset = len(closes) - len(oi)
            closes = closes[offset:]
            volumes = volumes[offset:]
            bar_idx = len(closes) - 1

        if bar_idx < V26_WARMUP:
            return

        self._signal_v26 = generate_signal_v26(closes, oi, volumes, bar_idx)
        self.state_map.signal_v26 = round(self._signal_v26, 3)

    # ══════════════════════════════════════════════════════════════════════
    #  H4 K线回调 (V29 — updates signal AND triggers trade decisions)
    # ══════════════════════════════════════════════════════════════════════

    def _on_bar_h4_update(self, kline: KLineData):
        self._push_widget(kline)

    def _on_bar_h4_complete(self, kline: KLineData):
        try:
            self._on_bar_h4(kline)
        except Exception as e:
            self.output(f"[H4异常] {type(e).__name__}: {e}")

    def _on_bar_h4(self, kline: KLineData):
        """H4 callback: compute V29 signal, combine with V26, manage positions."""
        p = self.params_map
        signal_price = 0.0

        # 撤挂单
        for oid in list(self.order_ids):
            self.cancel_order(oid)
        for oid in self._om.check_timeouts(self.cancel_order):
            self.output(f"[超时撤单] {oid}")

        # 历史回放
        if not self.trading:
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline)
            return

        # 执行pending (next-bar规则)
        if self._pending is not None:
            signal_price = self._execute(kline)
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # H4 数据准备
        producer_h4 = self.kline_gen_h4.producer
        closes = np.array(producer_h4.close, dtype=np.float64)
        highs = np.array(producer_h4.high, dtype=np.float64)
        lows = np.array(producer_h4.low, dtype=np.float64)
        volumes = np.array(producer_h4.volume, dtype=np.float64)
        bar_idx = len(closes) - 1

        if bar_idx < V29_WARMUP:
            self._push_widget(kline, signal_price)
            return

        close = closes[bar_idx]

        # V29 信号计算
        self._signal_v29 = generate_signal_v29(closes, highs, lows, volumes, bar_idx)
        self.state_map.signal_v29 = round(self._signal_v29, 3)

        # Combined signal (average of V26 and V29)
        combined = (self._signal_v26 + self._signal_v29) / 2.0
        forecast = min(FORECAST_CAP, max(0.0, abs(combined) * FORECAST_SCALAR))
        self.state_map.forecast = round(forecast, 1)

        # 仓位计算
        atr_arr = _atr(highs, lows, closes)
        optimal = calc_optimal_lots(
            forecast, atr_arr[bar_idx], close,
            p.capital, p.max_position, self._multiplier, ANNUAL_FACTOR,
        )
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        target = apply_buffer(optimal, current)

        # forecast=0 → 强制退出 (信号消失不走buffer)
        if forecast == 0 and current > 0:
            target = 0

        self.state_map.net_pos = current

        # 持仓追踪 (short: track trough_price = lowest since entry)
        if current == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0
        elif self.trough_price == 0.0 or close < self.trough_price:
            self.trough_price = close
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.trough_price = round(self.trough_price, 1)

        # 权益更新
        acct = self._get_account()
        pos_profit = 0.0
        if acct:
            self._risk.update(acct.balance)
            pos_profit = acct.position_profit
            self.state_map.equity = round(acct.balance, 0)
            self.state_map.drawdown = f"{self._risk.drawdown_pct:.2%}"
            self.state_map.daily_pnl = f"{self._risk.daily_pnl_pct:+.2%}"

        # 盘前清仓已禁用 — 完全靠信号和止损管理
        # # ── 盘前清仓 ──
        # if self._guard.should_flatten() and current > 0:
        #     self._pending = "FLATTEN"
        #     self._pending_target = 0
        #     self._pending_reason = "盘前清仓"
        #     self._push_widget(kline)
        #     return

        # ── 非交易时段 ──
        if not self._guard.should_trade():
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # ── 止损检查 (inline for short) ──
        if current > 0:
            # Hard stop: close >= avg_price * (1 + hard_stop_pct/100)
            if self.avg_price > 0 and close >= self.avg_price * (1 + p.hard_stop_pct / 100):
                self._pending = "HARD_STOP"
                self._pending_target = 0
                self._pending_reason = (
                    f"硬止损(空) close={close:.1f} >= "
                    f"avg*(1+{p.hard_stop_pct}%)={self.avg_price * (1 + p.hard_stop_pct / 100):.1f}"
                )

            # Trail stop: close >= trough_price * (1 + trailing_pct/100)
            if self.trough_price > 0 and close >= self.trough_price * (1 + p.trailing_pct / 100):
                self._pending = "TRAIL_STOP"
                self._pending_target = 0
                self._pending_reason = (
                    f"移动止损(空) close={close:.1f} >= "
                    f"trough*(1+{p.trailing_pct}%)={self.trough_price * (1 + p.trailing_pct / 100):.1f}"
                )

            # Equity/Portfolio stops (direction-agnostic)
            action, reason = self._risk.check(
                close=close, avg_price=self.avg_price, peak_price=self.avg_price,
                pos_profit=pos_profit, net_pos=current,
                hard_stop_pct=999.0, trailing_pct=999.0,
                equity_stop_pct=p.equity_stop_pct,
            )
            if action and action not in ("WARNING", "REDUCE", "HARD_STOP", "TRAIL_STOP"):
                self._pending = action
                self._pending_target = 0
                self._pending_reason = reason
            elif action == "REDUCE":
                target = max(0, current // 2)
                self._pending_reason = reason
            elif action == "WARNING":
                feishu("warning", p.instrument_id, f"**预警**: {reason}")

        # ── Chandelier Exit (Short, H4 data) ──
        if current > 0:
            ch_atr = _atr(highs, lows, closes, CHANDELIER_PERIOD)
            if chandelier_short(lows, closes, ch_atr, bar_idx):
                self._pending = "CLOSE"
                self._pending_target = 0
                self._pending_reason = "Chandelier Exit (Short)"

        # ── 正常信号 → pending ──
        if target != current:
            if current == 0 and target > 0:
                self._pending = "OPEN"
            elif target == 0:
                self._pending = "CLOSE"
            elif target > current:
                self._pending = "ADD"
            else:
                self._pending = "REDUCE"
            self._pending_target = target
            self._pending_reason = (
                f"v26={self._signal_v26:.2f} v29={self._signal_v29:.2f} "
                f"combined={combined:.2f} forecast={forecast:.1f} "
                f"optimal={optimal:.1f} target={target}"
            )

        self.state_map.target_lots = target

        # ── 当前bar立即处理pending (不等下一根bar) ──
        if self._pending is not None:
            signal_price = self._execute(kline)

        self.state_map.pending = self._pending or "---"
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ══════════════════════════════════════════════════════════════════════
    #  执行 (SHORT: open=sell, close=buy)
    # ══════════════════════════════════════════════════════════════════════

    def _execute(self, kline: KLineData) -> float:
        action = self._pending
        target = self._pending_target if self._pending_target is not None else 0
        reason = self._pending_reason
        self._pending = None
        self._pending_target = None
        self._pending_reason = ""

        p = self.params_map
        price = kline.close
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        diff = target - current

        if diff == 0:
            return 0.0

        # 保证金检查
        if diff > 0 and self._investor_id:
            acct = self.get_account_fund_data(self._investor_id)
            if acct and price * self._multiplier * diff * 0.15 > acct.available * 0.6:
                self.output("[保证金不足]")
                feishu("error", p.instrument_id, f"**保证金不足** 需{diff}手")
                return 0.0

        if diff > 0:
            # 开空 / 加空: send_order with direction="sell"
            self._slip.set_signal_price(price)
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=diff, price=price, order_direction="sell", market=False,
            )
            if oid is not None:
                self.order_ids.add(oid)
                self._om.on_send(oid, diff, price)
            if action == "OPEN":
                self.avg_price = price
                self.trough_price = price
            elif current > 0:
                self.avg_price = (self.avg_price * current + price * diff) / (current + diff)
        else:
            # 平空: auto_close_position with direction="buy"
            self._slip.set_signal_price(price)
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=abs(diff), price=price, order_direction="buy", market=False,
            )
            if oid is not None:
                self.order_ids.add(oid)
                self._om.on_send(oid, abs(diff), price)
            if target == 0:
                self._perf.on_close(self.avg_price, price, current, direction="short")
                self.avg_price = 0.0
                self.trough_price = 0.0

        self._rec(action, abs(diff), "卖" if diff > 0 else "买", price, current, target)
        label = {
            "OPEN": "开空", "ADD": "加空", "REDUCE": "减空", "CLOSE": "平空",
            "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损", "EQUITY_STOP": "权益止损",
            "CIRCUIT": "熔断", "DAILY_STOP": "单日止损", "FLATTEN": "清仓",
        }.get(action, action)

        feishu(action.lower(), p.instrument_id,
               f"**{label}** {abs(diff)}手 @ {price:,.1f}\n"
               f"逻辑: {reason}\n持仓: {current} → {target}")
        self.state_map.last_action = label
        self._save()
        return -price if diff > 0 else price

    # ══════════════════════════════════════════════════════════════════════
    #  辅助
    # ══════════════════════════════════════════════════════════════════════

    def _rec(self, action, lots, side, price, before, after):
        self._today_trades.append({
            "time": time.strftime("%H:%M:%S"), "action": action,
            "lots": lots, "side": side, "price": round(price, 1),
            "before": before, "after": after,
        })

    def _save(self):
        state = {
            "trough_price": self.trough_price,
            "avg_price": self.avg_price,
            "signal_v26": self._signal_v26,
            "signal_v29": self._signal_v29,
            "trading_day": self._current_td,
            "today_trades": self._today_trades[-50:],
        }
        state.update(self._risk.get_state())
        save_state(state, name=STRATEGY_NAME)

    def _send_review(self):
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        net = pos.net_position if pos else 0
        acct = self._get_account()
        eq = acct.balance if acct else 0
        feishu("daily_review", p.instrument_id,
               f"**{STRATEGY_NAME} 日报**\n权益: {eq:,.0f}\n"
               f"回撤: {self._risk.drawdown_pct:.2%}\n"
               f"日PnL: {self._risk.daily_pnl_pct:+.2%}\n持仓: {net}手\n"
               f"V26={self._signal_v26:.2f} V29={self._signal_v29:.2f}")

    def _push_widget(self, kline, sp=0.0):
        try:
            self.widget.recv_kline({
                "kline": kline, "signal_price": sp, **self.main_indicator_data,
            })
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════
    #  回调
    # ══════════════════════════════════════════════════════════════════════

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_ids.discard(trade.order_id)
        self._om.on_fill(trade.order_id)
        self._slip.on_fill(trade.price, trade.volume,
                           ("buy" if str(trade.direction).lower() in ("buy", "0", "买") else "sell"))
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        actual = abs(pos.net_position) if pos else 0
        direction = ("buy" if str(trade.direction).lower() in ("buy", "0", "买") else "sell")
        if direction == "sell" and actual > 0:
            old_pos = max(0, actual - trade.volume)
            if old_pos > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old_pos + trade.price * trade.volume) / actual
            else:
                self.avg_price = trade.price
        elif direction == "buy" and actual == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0
        self.state_map.net_pos = self.get_position(
            self.params_map.instrument_id).net_position
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_ids.discard(order.order_id)
        self._om.on_cancel(order.order_id)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
        throttle_on_error(self, error)
