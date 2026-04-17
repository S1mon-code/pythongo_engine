"""
================================================================================
  AL_Long_1H_V8 — Donchian Breakout + ADX Trend Filter (电解铝 H1做多)
================================================================================

  QBase_v3 策略: i_long_v8_donchian_adx_filter
  研究来源: research/long/I/1h/v8_+52.40%
  信号: Donchian通道突破 + ADX趋势强度确认(>22) + PDI>MDI方向确认 → [0, 1]
  执行: VWAP — 买低于VWAP、卖高于VWAP, 匹配QBase pipeline/vwap_executor.py
  止损: Chandelier Exit + RiskManager(移动/硬/权益止损 + Portfolio Stops)
  仓位: Vol Targeting + Carver 10% buffer
  部署: modules/ → pyStrategy/modules/, 本文件 → self_strategy/

================================================================================
"""
import time
from datetime import datetime

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

# ── 模块导入 ──
from modules.contract_info import get_multiplier, get_tick_size
from modules.session_guard import SessionGuard
from modules.feishu import feishu
from modules.persistence import save_state, load_state
from modules.trading_day import get_trading_day, is_new_day, DAY_START_HOUR
from modules.risk import check_stops, RiskManager
from modules.slippage import SlippageTracker
from modules.heartbeat import HeartbeatMonitor
from modules.execution import EntryParams, EntryState, ExecAction, ScaledEntryExecutor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.pricing import AggressivePricer
from modules.rolling_vwap import RollingVWAP
from modules.rollover import check_rollover
from modules.position_sizing import calc_optimal_lots, apply_buffer


def _freq_to_sec(kline_style: str) -> int:
    """kline_style → 秒数 (bar 周期)."""
    mapping = {
        "M1": 60, "M3": 180, "M5": 300, "M15": 900, "M30": 1800,
        "H1": 3600, "H2": 7200, "H4": 14400,
        "D1": 86400, "W1": 604800,
    }
    return mapping.get(str(kline_style).upper(), 3600)

# 止损动作集 (止损立即执行, 不走VWAP)
IMMEDIATE_ACTIONS = frozenset({
    "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP", "CIRCUIT", "DAILY_STOP", "FLATTEN",
})


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "AL_Long_1H_V8"

# 策略指标参数 (QBase_v3 research/long/I/1h/v8_+52.40%)
DC_PERIOD = 40
ADX_PERIOD = 14
ADX_THRESHOLD = 22.0
SIGNAL_SCALE = 1.5
WARMUP = 80

# Chandelier Exit (优化值, 来自 params.yaml)
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 2.58

# Vol Targeting
FORECAST_SCALAR = 10.0
FORECAST_CAP = 20.0
ANNUAL_FACTOR = 252 * 8            # H1: AL有夜盘, ~8 bars/day

# VWAP执行参数
VWAP_MIN_WAIT_SEC = 30             # 两次下单最小间隔(秒)
VWAP_FORCE_MINUTE = 50             # 每小时第50分钟起强制成交

# 日报时间
DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS (inline, 纯numpy, 从QBase移植)
# ══════════════════════════════════════════════════════════════════════════════

def _atr(highs, lows, closes, period=14):
    """ATR — Wilder RMA."""
    n = len(closes)
    if n == 0 or n < period + 1:
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


def _donchian(highs, lows, period=20):
    """Donchian Channel — returns (upper, lower, mid). Excludes current bar."""
    n = len(highs)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    mid = np.full(n, np.nan)
    for i in range(period, n):
        upper[i] = np.max(highs[i - period:i])
        lower[i] = np.min(lows[i - period:i])
        mid[i] = (upper[i] + lower[i]) / 2.0
    return upper, lower, mid


def _adx_with_di(highs, lows, closes, period=14):
    """ADX with directional indicators — returns (adx, plus_di, minus_di)."""
    n = len(highs)
    nans = np.full(n, np.nan)
    if n == 0 or n < period + 1:
        return nans.copy(), nans.copy(), nans.copy()

    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    high_low = highs[1:] - lows[1:]
    high_close = np.abs(highs[1:] - closes[:-1])
    low_close = np.abs(lows[1:] - closes[:-1])
    tr = np.maximum(high_low, np.maximum(high_close, low_close))

    def _wilder(values, p):
        out = np.full(len(values), np.nan)
        if len(values) < p:
            return out
        out[p - 1] = np.sum(values[:p])
        for i in range(p, len(values)):
            out[i] = out[i - 1] - out[i - 1] / p + values[i]
        return out

    sm_plus = _wilder(plus_dm, period)
    sm_minus = _wilder(minus_dm, period)
    sm_tr = _wilder(tr, period)

    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    valid = sm_tr > 0
    idx = np.where(valid)[0]
    plus_di[idx + 1] = 100.0 * sm_plus[idx] / sm_tr[idx]
    minus_di[idx + 1] = 100.0 * sm_minus[idx] / sm_tr[idx]

    di_sum = plus_di + minus_di
    di_diff = np.abs(plus_di - minus_di)
    dx = np.full(n, np.nan)
    nonzero = di_sum > 0
    dx[nonzero] = 100.0 * di_diff[nonzero] / di_sum[nonzero]

    adx_out = np.full(n, np.nan)
    first_valid = period
    dx_valid = dx[first_valid:]
    if len(dx_valid) < period:
        return adx_out, plus_di, minus_di
    adx_start = first_valid + period - 1
    adx_out[adx_start] = np.mean(dx_valid[:period])
    for i in range(adx_start + 1, n):
        adx_out[i] = (adx_out[i - 1] * (period - 1) + dx[i]) / period
    return adx_out, plus_di, minus_di


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL — Donchian breakout + ADX trend filter
# ══════════════════════════════════════════════════════════════════════════════

def generate_signal(closes, highs, lows, bar_idx):
    """Donchian突破 + ADX趋势确认 + PDI>MDI → [0, 1]. Long only."""
    if bar_idx < WARMUP:
        return 0.0

    dc_upper, dc_lower, dc_mid = _donchian(highs, lows, DC_PERIOD)
    adx_arr, pdi_arr, mdi_arr = _adx_with_di(highs, lows, closes, ADX_PERIOD)

    close = closes[bar_idx]
    upper = dc_upper[bar_idx]
    mid = dc_mid[bar_idx]
    a = adx_arr[bar_idx]
    pdi = pdi_arr[bar_idx]
    mdi = mdi_arr[bar_idx]

    if any(np.isnan(x) for x in (upper, mid, a, pdi, mdi)):
        return 0.0
    if a < ADX_THRESHOLD or pdi <= mdi:
        return 0.0
    if close <= mid:
        return 0.0
    width = upper - mid
    if width <= 0:
        return 0.0
    penetration = (close - mid) / width
    return float(np.clip(penetration * SIGNAL_SCALE, 0.0, 1.0))


# ══════════════════════════════════════════════════════════════════════════════
#  CHANDELIER EXIT (LONG)
# ══════════════════════════════════════════════════════════════════════════════

def chandelier_long(highs, closes, atr_arr, bar_idx):
    """Long Chandelier: close < highest_high(period) - mult x ATR."""
    if bar_idx < CHANDELIER_PERIOD:
        return False
    a = atr_arr[bar_idx]
    if np.isnan(a):
        return False
    hh = np.max(highs[bar_idx - CHANDELIER_PERIOD + 1:bar_idx + 1])
    return bool(closes[bar_idx] < hh - CHANDELIER_MULT * a)


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="SHFE", title="交易所代码")
    instrument_id: str = Field(default="al2605", title="合约代码")
    kline_style: str = Field(default="H1", title="K线周期")
    max_lots: int = Field(default=10, title="最大持仓")
    capital: float = Field(default=1_000_000, title="配置资金")
    hard_stop_pct: float = Field(default=0.5, title="硬止损(%)")
    trailing_pct: float = Field(default=0.3, title="移动止损(%)")
    equity_stop_pct: float = Field(default=2.0, title="权益止损(%)")
    flatten_minutes: int = Field(default=5, title="即将收盘提示(分钟)")
    sim_24h: bool = Field(default=False, title="24H模拟盘模式")


class State(BaseState):
    signal: float = Field(default=0.0, title="信号")
    forecast: float = Field(default=0.0, title="预测")
    target_lots: int = Field(default=0, title="目标手")
    net_pos: int = Field(default=0, title="净持仓")
    avg_price: float = Field(default=0.0, title="均价")
    peak_price: float = Field(default=0.0, title="峰价")
    hard_line: float = Field(default=0.0, title="止损线")
    trail_line: float = Field(default=0.0, title="移损线")
    equity: float = Field(default=0.0, title="权益")
    drawdown: str = Field(default="---", title="回撤")
    daily_pnl: str = Field(default="---", title="当日盈亏")
    trading_day: str = Field(default="", title="交易日")
    session: str = Field(default="---", title="交易时段")
    pending: str = Field(default="---", title="待执行")
    last_action: str = Field(default="---", title="上次操作")
    slippage: str = Field(default="---", title="滑点")
    perf: str = Field(default="---", title="绩效")
    vwap: str = Field(default="---", title="VWAP执行")
    entry_progress: str = Field(default="---", title="入场进度")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

class AL_Long_1H_V8_Donchian_ADX_Filter(BaseStrategy):
    """电解铝 H1做多 — Donchian Breakout + ADX + VWAP执行"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        # 持仓状态
        self.avg_price = 0.0
        self.peak_price = 0.0
        self._pending = None
        self._pending_target = None
        self._pending_reason = ""
        self.order_id = set()

        # VWAP执行状态 (替代TWAP, 匹配QBase vwap_executor.py)
        self._vwap_active = False       # 是否有正在执行的VWAP订单
        self._vwap_remaining = 0        # 剩余手数 (正=买, 负=卖)
        self._vwap_direction = ""       # "buy" or "sell"
        self._vwap_action = ""          # OPEN/ADD/REDUCE/CLOSE
        self._vwap_reason = ""
        self._vwap_cum_pv = 0.0         # 当前执行窗口: 累计 price * delta_vol
        self._vwap_cum_vol = 0.0        # 当前执行窗口: 累计 delta_vol
        self._vwap_prev_vol = 0         # 上一tick的累计成交量 (用于算delta)
        self._vwap_fill_pv = 0.0        # 成交追踪: 累计 fill_price * fill_lots
        self._vwap_fill_lots = 0        # 成交追踪: 累计 fill_lots
        self._vwap_last_send = 0.0      # 限流: 上次发单时间戳

        # 权益追踪
        self._investor_id = ""
        self._risk = None
        self._current_td = ""
        self._daily_review_sent = False
        self._rollover_checked = False
        self._today_trades = []

        # 模块实例
        self._guard = None
        self._slip = None
        self._hb = None
        self._om = OrderMonitor()
        self._perf = None
        self._pricer: AggressivePricer | None = None   # on_start 时根据 tick_size 初始化
        self._multiplier = 5

        # Scaled entry executor (2026-04-17) — 替代 VWAP 入场
        self._rvwap: RollingVWAP | None = None
        self._entry: ScaledEntryExecutor | None = None
        self._rvwap_prev_vol = 0

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
        self._pricer = AggressivePricer(tick_size=get_tick_size(p.instrument_id))
        # Scaled entry infrastructure (2026-04-17)
        self._rvwap = RollingVWAP(window_seconds=1800)
        self._entry = ScaledEntryExecutor(EntryParams(bottom_lots=2))
        self._rvwap_prev_vol = 0

        self._guard = SessionGuard(p.instrument_id, p.flatten_minutes, sim_24h=p.sim_24h)
        self._slip = SlippageTracker(p.instrument_id)
        self._hb = HeartbeatMonitor(p.instrument_id)
        self._perf = PerformanceTracker(p.instrument_id)

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self.kline_generator.push_history_data()

        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id

        self._risk = RiskManager(capital=p.capital)

        saved = load_state(STRATEGY_NAME)
        if saved:
            self._risk.load_state(saved)
            self.peak_price = saved.get("peak_price", 0.0)
            self.avg_price = saved.get("avg_price", 0.0)
            self.state_map.signal = saved.get("signal", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._today_trades = saved.get("today_trades", [])
            self.output(f"[恢复] peak_eq={self._risk.peak_equity:.0f} avg={self.avg_price:.1f}")

        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance)

        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        level, days = check_rollover(p.instrument_id)
        if level:
            feishu("rollover", p.instrument_id, f"**换月提醒**: 距交割月**{days}天**")

        super().on_start()
        self.output(
            f"启动 | {p.instrument_id} {p.kline_style} | "
            f"乘数={self._multiplier} | 持仓={actual}"
        )
        feishu("start", p.instrument_id,
               f"**策略启动** {STRATEGY_NAME}\n合约: {p.instrument_id}\n"
               f"乘数: {self._multiplier}\n持仓: {actual}手\n执行: VWAP")

    def on_stop(self):
        self._save()
        feishu("shutdown", self.params_map.instrument_id,
               f"**策略停止** {STRATEGY_NAME}\n持仓: {self.state_map.net_pos}手\n"
               f"{self._slip.format_report()}")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)

        # 先喂 pricer + RollingVWAP, 后续 stops/entry 都依赖它们
        if self._pricer is not None:
            try:
                self._pricer.update(tick)
            except Exception as e:
                self.output(f"[pricer异常] {type(e).__name__}: {e}")

        if self._rvwap is not None:
            try:
                self._rvwap.update(tick.last_price, tick.volume, datetime.now())
            except Exception as e:
                self.output(f"[rvwap异常] {type(e).__name__}: {e}")

        try:
            self._on_tick_stops(tick)
        except Exception as e:
            self.output(f"[stops异常] {type(e).__name__}: {e}")

        # 新 Scaled Entry 驱动 (2026-04-17) — 替代旧 _on_tick_vwap
        try:
            self._drive_entry(tick)
        except Exception as e:
            self.output(f"[entry异常] {type(e).__name__}: {e}")
            feishu("error", self.params_map.instrument_id,
                   f"**entry 异常**\n{type(e).__name__}: {e}")

        # 旧 VWAP 路径保留但不再被信号触发激活 (safety fallback);
        # _vwap_active 永不为 True (_submit_vwap 已被短路),因此无副作用
        try:
            self._on_tick_vwap(tick)
        except Exception as e:
            self.output(f"[VWAP异常] {type(e).__name__}: {e}")

        try:
            self._on_tick_aux(tick)
        except Exception as e:
            self.output(f"[on_tick异常] {type(e).__name__}: {e}")
            feishu("error", self.params_map.instrument_id,
                   f"**on_tick异常**\n{type(e).__name__}: {e}")

    def _on_tick_stops(self, tick: TickData):
        """Tick 级止损检查 (2026-04-17 重构).

        - peak/trough 每 tick 更新
        - 硬止损每 tick 判断, 立即触发
        - 移动止损每分钟判断一次 (降噪)
        """
        if not self.trading:
            return
        if self._guard is not None and not self._guard.should_trade():
            return
        if self._pending is not None:
            return  # 已有 pending, 让 _on_bar 处理
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        if pos is None:
            return
        net_pos = pos.net_position
        price = tick.last_price

        self._risk.update_peak_trough_tick(price, net_pos)
        self.peak_price = self._risk.peak_price  # 同步给 state_map / save

        if net_pos <= 0:
            return

        # 硬止损 — tick 级
        action, reason = self._risk.check_hard_stop_tick(
            price=price, avg_price=self.avg_price,
            net_pos=net_pos, hard_stop_pct=p.hard_stop_pct,
        )
        if action:
            self.output(f"[{action}][TICK] {reason}")
            self._exec_stop_at_tick(price, action, reason)
            if self._entry is not None:
                stop_actions = self._entry.on_stop_triggered(datetime.now())
                for ea in stop_actions:
                    self._apply_entry_action(ea)
            return

        # 移动止损 — 每分钟
        action, reason = self._risk.check_trail_minutely(
            price=price, now=datetime.now(),
            net_pos=net_pos, trailing_pct=p.trailing_pct,
        )
        if action:
            self.output(f"[{action}][M1] {reason}")
            self._exec_stop_at_tick(price, action, reason)
            if self._entry is not None:
                stop_actions = self._entry.on_stop_triggered(datetime.now())
                for ea in stop_actions:
                    self._apply_entry_action(ea)

    def _on_tick_vwap(self, tick: TickData):
        """VWAP执行: 买低于VWAP, 卖高于VWAP, 匹配QBase vwap_executor."""
        if not self._vwap_active:
            return

        # 非交易时段一律不挂单 (SHFE pre-opening会拒单/拒撤)
        if self._guard is not None and not self._guard.should_trade():
            return

        price = tick.last_price
        now = time.time()

        # ── 更新running VWAP (从tick累计成交量的delta计算) ──
        cur_vol = tick.volume
        if self._vwap_prev_vol == 0:
            self._vwap_prev_vol = cur_vol
            return
        delta_vol = cur_vol - self._vwap_prev_vol
        if delta_vol > 0:
            self._vwap_cum_pv += price * delta_vol
            self._vwap_cum_vol += delta_vol
        self._vwap_prev_vol = cur_vol

        # VWAP不够数据 (刚开始执行)
        if self._vwap_cum_vol <= 0:
            return

        vwap = self._vwap_cum_pv / self._vwap_cum_vol

        # ── 检查是否完成 ──
        if self._vwap_remaining == 0:
            self._vwap_complete()
            return

        # ── 限流: 两次下单间隔至少 VWAP_MIN_WAIT_SEC ──
        if now - self._vwap_last_send < VWAP_MIN_WAIT_SEC:
            return

        # ── 强制成交: 每小时第50分钟起 ──
        force = datetime.now().minute >= VWAP_FORCE_MINUTE

        # ── 下单量: 剩余的一半, 最少1手 ──
        size = max(1, abs(self._vwap_remaining) // 2)
        p = self.params_map

        # VWAP 普通批次用 cross,强制模式用 urgent (escalator 也会自然升级)
        vwap_urgency = "urgent" if force else "cross"
        if self._vwap_remaining > 0:
            # 需要买: 价格低于VWAP时买, 或强制成交
            if price < vwap or force:
                batch = min(self._vwap_remaining, size)
                buy_price = self._aggressive_price(price, "buy", urgency=vwap_urgency)
                oid = self.send_order(
                    exchange=p.exchange, instrument_id=p.instrument_id,
                    volume=batch, price=buy_price, order_direction="buy",
                )
                if oid is not None:
                    self.order_id.add(oid)
                    self._om.on_send(oid, batch, buy_price,
                                     urgency=vwap_urgency, direction="buy", kind="open")
                    self._vwap_remaining -= batch
                    self._vwap_last_send = now
                self.output(
                    f"[VWAP] buy {batch}手 @ {buy_price:.1f} "
                    f"vwap={vwap:.1f} remain={self._vwap_remaining}"
                    f"{' (FORCE)' if force else ''}"
                )

        elif self._vwap_remaining < 0:
            # 需要卖: 价格高于VWAP时卖, 或强制成交
            batch = min(abs(self._vwap_remaining), size)
            if price > vwap or force:
                sell_price = self._aggressive_price(price, "sell", urgency=vwap_urgency)
                oid = self.auto_close_position(
                    exchange=p.exchange, instrument_id=p.instrument_id,
                    volume=batch, price=sell_price, order_direction="sell",
                )
                if oid is not None:
                    self.order_id.add(oid)
                    self._om.on_send(oid, batch, sell_price,
                                     urgency=vwap_urgency, direction="sell", kind="close")
                    self._vwap_remaining += batch
                    self._vwap_last_send = now
                self.output(
                    f"[VWAP] sell {batch}手 @ {price:.1f} "
                    f"vwap={vwap:.1f} remain={self._vwap_remaining}"
                    f"{' (FORCE)' if force else ''}"
                )

        # 更新UI
        exec_vwap = self._vwap_fill_pv / self._vwap_fill_lots if self._vwap_fill_lots > 0 else 0
        self.state_map.vwap = (
            f"{self._vwap_action} "
            f"{self._vwap_fill_lots}/{self._vwap_fill_lots + abs(self._vwap_remaining)} "
            f"VWAP={vwap:.1f}"
        )

    def _submit_vwap(self, kline: KLineData, action: str):
        """启动VWAP执行窗口."""
        p = self.params_map
        # 非交易时段不启动VWAP (避免SHFE pre-opening拒单)
        if self._guard is not None and not self._guard.should_trade():
            self.output(f"[VWAP跳过] 非交易时段, 延后 {action}")
            return
        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0

        if action == "OPEN":
            vol = max(1, self._pending_target or 1)
            direction = "buy"
        elif action == "ADD":
            vol = max(1, (self._pending_target or (actual + 1)) - actual)
            direction = "buy"
        elif action == "REDUCE":
            vol = max(1, actual - (self._pending_target or (actual // 2)))
            direction = "sell"
        elif action == "CLOSE":
            vol = actual
            direction = "sell"
        else:
            return

        if vol <= 0:
            return

        if direction == "buy":
            acct = self._get_account()
            price = kline.close
            if acct and price * self._multiplier * vol * 0.15 > acct.available * 0.6:
                self.output("[保证金不足] VWAP取消")
                feishu("error", p.instrument_id, f"**保证金不足** VWAP {action} {vol}手")
                return

        # 初始化VWAP执行状态
        self._vwap_active = True
        self._vwap_remaining = vol if direction == "buy" else -vol
        self._vwap_direction = direction
        self._vwap_action = action
        self._vwap_reason = self._pending_reason
        self._vwap_cum_pv = 0.0
        self._vwap_cum_vol = 0.0
        self._vwap_prev_vol = 0
        self._vwap_fill_pv = 0.0
        self._vwap_fill_lots = 0
        self._vwap_last_send = 0.0

        self.output(f"[VWAP提交] {action} {vol}手 {direction}")
        feishu("info", p.instrument_id,
               f"**VWAP启动** {action}\n目标: {vol}手 {direction}\n"
               f"模式: 买<VWAP 卖>VWAP, 第50分钟起强制\n逻辑: {self._pending_reason}")

    def _vwap_cancel(self):
        """取消进行中的VWAP执行."""
        if not self._vwap_active:
            return
        self.output(f"[VWAP取消] {self._vwap_action} 已成交{self._vwap_fill_lots}手")
        self._vwap_active = False
        self._vwap_remaining = 0

    def _vwap_complete(self):
        """VWAP执行完成."""
        exec_vwap = self._vwap_fill_pv / self._vwap_fill_lots if self._vwap_fill_lots > 0 else 0
        feishu("info", self.params_map.instrument_id,
               f"**VWAP完成** {self._vwap_action}\n"
               f"成交: {self._vwap_fill_lots}手 VWAP={exec_vwap:.1f}")
        self.output(f"[VWAP完成] {self._vwap_action} {self._vwap_fill_lots}手 VWAP={exec_vwap:.1f}")
        self._vwap_active = False
        self.state_map.vwap = "---"

    def _on_tick_aux(self, tick: TickData):
        p = self.params_map

        # ── 未成交订单 urgency 升级 ──
        if self._guard is not None and self._guard.should_trade() and self._pricer is not None:
            to_escalate = self._om.check_escalation()
            for oid, next_urgency, info in to_escalate:
                self._resubmit_escalated(oid, next_urgency, info)

        td = get_trading_day()
        if td != self._current_td and self._current_td:
            acct = self._get_account()
            if acct:
                self._risk.on_day_change(acct.balance)
            self._perf.on_day_change()
            self._today_trades = []
            self._current_td = td
            self.state_map.trading_day = td
            self._daily_review_sent = False
            self._rollover_checked = False
            self._save()
            self.output(f"[新交易日] {td} (21:00 day start)")
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

    # ══════════════════════════════════════════════════════════════════════
    #  Scaled Entry (2026-04-17) — 替代 VWAP 入场路径
    # ══════════════════════════════════════════════════════════════════════

    def _drive_entry(self, tick: TickData) -> None:
        """每 tick 驱动 entry executor."""
        if self._entry is None or self._rvwap is None or self._pricer is None:
            return
        if not self.trading:
            return
        if self._guard is not None and not self._guard.should_trade():
            return

        p = self.params_map
        pos = self.get_position(p.instrument_id)
        net_pos = pos.net_position if pos else 0

        actions = self._entry.on_tick(
            now=datetime.now(),
            last_price=tick.last_price,
            bid1=self._pricer.bid1,
            ask1=self._pricer.ask1,
            tick_size=self._pricer.tick_size,
            vwap_value=self._rvwap.value,
            forecast=self.state_map.forecast,
            current_position=net_pos,
        )
        for a in actions:
            self._apply_entry_action(a)

        # UI
        self.state_map.entry_progress = self._entry.progress_str()

    def _apply_entry_action(self, a: ExecAction) -> None:
        """策略层执行 executor 返回的动作."""
        p = self.params_map
        if a.op == "submit":
            if a.kind == "open":
                oid = self.send_order(
                    exchange=p.exchange, instrument_id=p.instrument_id,
                    volume=a.vol, price=a.price, order_direction=a.direction,
                )
            else:
                oid = self.auto_close_position(
                    exchange=p.exchange, instrument_id=p.instrument_id,
                    volume=a.vol, price=a.price, order_direction=a.direction,
                )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, a.vol, a.price,
                                 urgency="entry",
                                 direction=a.direction, kind=a.kind)
                self._entry.register_pending(oid, a.vol, price=a.price)
                self.output(
                    f"[ENTRY] {a.direction} {a.vol}手 @ {a.price:.1f} "
                    f"urgency={a.urgency_score:.2f} state={self._entry.state.value}"
                )
            else:
                self.output(f"[ENTRY] 发单失败 {a.direction} {a.vol}手 @ {a.price:.1f}")

        elif a.op == "cancel":
            if a.oid is not None:
                self.cancel_order(a.oid)
                self._entry.register_cancelled(a.oid)
                self.order_id.discard(a.oid)

        elif a.op == "cancel_all":
            for oid in list(self._entry.pending_oids.keys()):
                self.cancel_order(oid)
                self._entry.register_cancelled(oid)
                self.order_id.discard(oid)

        elif a.op == "feishu":
            feishu("info", p.instrument_id, a.note)

    # ══════════════════════════════════════════════════════════════════════
    #  K线回调
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        try:
            self._on_bar(kline)
        except Exception as e:
            self.output(f"[callback异常] {type(e).__name__}: {e}")

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    def _on_bar(self, kline: KLineData):
        signal_price = 0.0
        p = self.params_map

        # 历史回放
        if not self.trading:
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline)
            return

        # 非交易时段: 不撤单、不下单、不生成新信号 (SHFE pre-opening会拒单/拒撤)
        # pending保留, 等交易时段开盘后下一根bar处理
        if self._guard is not None and not self._guard.should_trade():
            self.state_map.session = self._guard.get_status()
            self._push_widget(kline)
            self.update_status_bar()
            return

        # 撤挂单 (VWAP进行中不撤) — 同步 executor pending_oids
        if not self._vwap_active:
            for oid in list(self.order_id):
                self.cancel_order(oid)
                if self._entry is not None:
                    self._entry.register_cancelled(oid)
            for oid in self._om.check_timeouts(self.cancel_order):
                self.output(f"[超时撤单] {oid}")
                if self._entry is not None:
                    self._entry.register_cancelled(oid)

        # 安全网: 处理上一根bar残留的pending
        if self._pending is not None:
            action = self._pending
            if action in IMMEDIATE_ACTIONS:
                if self._vwap_active:
                    self._vwap_cancel()
                    for oid in list(self.order_id):
                        self.cancel_order(oid)
                        if self._entry is not None:
                            self._entry.register_cancelled(oid)
                    self.output(f"[VWAP取消+撤单] 止损优先: {action}")
                signal_price = self._execute(kline, action)
            elif action in ("OPEN", "ADD") and self._entry is not None:
                # 2026-04-17: 残留 OPEN/ADD → 重新走 executor 入场
                net_pos = self.get_position(self.params_map.instrument_id).net_position
                bar_total = _freq_to_sec(self.params_map.kline_style)
                actions = self._entry.on_signal(
                    target=self._pending_target or 1, direction="buy",
                    now=datetime.now(), current_position=net_pos,
                    forecast=self.state_map.forecast, bar_total_sec=bar_total,
                )
                for ea in actions:
                    self._apply_entry_action(ea)
                signal_price = 0.0
            elif self._vwap_active:
                self.output(f"[VWAP进行中] 忽略pending {action}")
                signal_price = 0.0
            else:
                self._submit_vwap(kline, action)
                signal_price = 0.0
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # VWAP进行中 → 不产生新信号, 但仍需检查止损
        if self._vwap_active:
            self.output(
                f"[VWAP进行中] {self._vwap_action} "
                f"remain={abs(self._vwap_remaining)} filled={self._vwap_fill_lots}"
            )

        # ── 数据准备 ──
        producer = self.kline_generator.producer
        if len(producer.close) < WARMUP + 2:
            self._push_widget(kline, signal_price)
            return

        closes = np.array(producer.close, dtype=np.float64)
        highs = np.array(producer.high, dtype=np.float64)
        lows = np.array(producer.low, dtype=np.float64)
        bar_idx = len(closes) - 1
        close = float(closes[-1])

        # ── 指标调试输出 ──
        dc_upper, _, dc_mid = _donchian(highs, lows, DC_PERIOD)
        adx_arr, pdi_arr, mdi_arr = _adx_with_di(highs, lows, closes, ADX_PERIOD)
        dc_u = dc_upper[bar_idx] if not np.isnan(dc_upper[bar_idx]) else 0.0
        dc_m = dc_mid[bar_idx] if not np.isnan(dc_mid[bar_idx]) else 0.0
        adx_v = adx_arr[bar_idx] if not np.isnan(adx_arr[bar_idx]) else 0.0
        pdi_v = pdi_arr[bar_idx] if not np.isnan(pdi_arr[bar_idx]) else 0.0
        mdi_v = mdi_arr[bar_idx] if not np.isnan(mdi_arr[bar_idx]) else 0.0
        self.output(
            f"[IND] DC_U={dc_u:.1f} DC_M={dc_m:.1f} "
            f"ADX={adx_v:.1f} PDI={pdi_v:.1f} MDI={mdi_v:.1f} close={close:.1f}"
        )

        # ── 信号计算 ──
        raw = generate_signal(closes, highs, lows, bar_idx)
        forecast = min(FORECAST_CAP, max(0.0, raw * FORECAST_SCALAR))
        self.state_map.signal = round(raw, 3)
        self.state_map.forecast = round(forecast, 1)
        self.output(f"[SIGNAL] raw={raw:.4f} forecast={forecast:.1f}")

        # ── 仓位计算 ──
        atr_arr = _atr(highs, lows, closes)
        optimal_raw = calc_optimal_lots(
            forecast, atr_arr[bar_idx], close,
            p.capital, p.max_lots, self._multiplier, ANNUAL_FACTOR,
        )
        optimal = round(optimal_raw)
        net_pos = self.get_position(p.instrument_id).net_position
        target = apply_buffer(optimal, net_pos)
        target = min(target, p.max_lots)
        # forecast=0 → 强制退出 (信号消失不走buffer)
        if forecast == 0 and net_pos > 0:
            target = 0
        self.state_map.net_pos = net_pos
        self.state_map.target_lots = target

        # ── 持仓追踪 (peak 由 _on_tick_stops 维护,此处只同步显示) ──
        if net_pos == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0
        else:
            # bar close 也参与 peak 更新, 避免 tick 丢失时峰值漂移
            self._risk.update_peak_trough_tick(close, net_pos)
            self.peak_price = self._risk.peak_price
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.peak_price = round(self.peak_price, 1)
        self.state_map.hard_line = (
            round(self.avg_price * (1 - p.hard_stop_pct / 100), 1) if net_pos > 0 else 0.0
        )
        self.state_map.trail_line = (
            round(self.peak_price * (1 - p.trailing_pct / 100), 1) if net_pos > 0 else 0.0
        )

        # ── 权益 ──
        acct = self._get_account()
        equity = pos_profit = 0.0
        if acct:
            equity = acct.balance
            pos_profit = acct.position_profit
            self._risk.update(equity)
            self.state_map.equity = round(equity, 0)
            self.state_map.drawdown = f"{self._risk.drawdown_pct:.2%}"
            self.state_map.daily_pnl = f"{self._risk.daily_pnl_pct:+.2%}"

        # ── 止损检查 (hard/trail 由 _on_tick_stops 处理, bar 级只负责 equity/portfolio/daily) ──
        if net_pos > 0:
            action, reason = self._risk.check(
                close=close, avg_price=self.avg_price, peak_price=self.peak_price,
                pos_profit=pos_profit, net_pos=net_pos,
                hard_stop_pct=999.0,     # tick 层已处理
                trailing_pct=999.0,      # tick/分钟层已处理
                equity_stop_pct=p.equity_stop_pct,
            )
            if action and action not in ("WARNING", "REDUCE"):
                self._pending = action
                self._pending_reason = reason
                self.output(f"[{action}] {reason}")
            elif action == "REDUCE":
                target = max(0, net_pos // 2)
                self._pending_reason = reason
                self.output(f"[REDUCE] {reason}")
            elif action == "WARNING":
                self.output(f"[预警] {reason}")
                feishu("warning", p.instrument_id, f"**回撤预警**: {reason}")

        # ── Chandelier Exit (Long) ──
        if self._pending is None and net_pos > 0:
            ch_atr = _atr(highs, lows, closes, CHANDELIER_PERIOD)
            if chandelier_long(highs, closes, ch_atr, bar_idx):
                self._pending = "CLOSE"
                self._pending_reason = "Chandelier Exit (Long)"
                self.output(f"[CHANDELIER] {self._pending_reason}")

        # ── 正常信号 → pending (VWAP进行中不产生新正常信号) ──
        if self._pending is None and not self._vwap_active and target != net_pos:
            if net_pos == 0 and target > 0:
                self._pending = "OPEN"
            elif target == 0 and net_pos > 0:
                self._pending = "CLOSE"
            elif target > net_pos:
                self._pending = "ADD"
            elif target < net_pos:
                self._pending = "REDUCE"
            self._pending_target = target
            self._pending_reason = (
                f"signal={raw:.2f} forecast={forecast:.1f} "
                f"optimal={optimal} target={target}"
            )

        # ── 当前bar立即处理pending (不等下一根bar) ──
        if self._pending is not None:
            action = self._pending
            if action in IMMEDIATE_ACTIONS:
                if self._vwap_active:
                    self._vwap_cancel()
                    for oid in list(self.order_id):
                        self.cancel_order(oid)
                        if self._entry is not None:
                            self._entry.register_cancelled(oid)
                    self.output(f"[VWAP取消+撤单] 止损优先: {action}")
                signal_price = self._execute(kline, action)
            elif action in ("OPEN", "ADD") and self._entry is not None:
                # 2026-04-17: 入场路径走 ScaledEntryExecutor
                # 保证金预检(audit fix)
                target_vol = self._pending_target or 1
                acct = self._get_account()
                cur_price = kline.close
                if acct and cur_price * self._multiplier * target_vol * 0.15 > acct.available * 0.6:
                    self.output(f"[ENTRY] 保证金不足, 跳过 {action}")
                    feishu("error", self.params_map.instrument_id,
                           f"**ENTRY 保证金不足** {action} 目标 {target_vol}手")
                    signal_price = 0.0
                else:
                    bar_total = _freq_to_sec(self.params_map.kline_style)
                    actions = self._entry.on_signal(
                        target=target_vol, direction="buy",
                        now=datetime.now(), current_position=net_pos,
                        forecast=forecast, bar_total_sec=bar_total,
                    )
                    for ea in actions:
                        self._apply_entry_action(ea)
                    feishu("info", self.params_map.instrument_id,
                           f"**ENTRY 启动** {action}\n"
                           f"目标: {target_vol}手 buy (delta 以持仓为准)\n"
                           f"逻辑: {self._pending_reason}")
                    signal_price = 0.0
            elif not self._vwap_active:
                # REDUCE / CLOSE 走原 VWAP 分批(暂时保留)
                self._submit_vwap(kline, action)
            else:
                self.output(f"[VWAP进行中] 忽略pending {action}")
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""

        self.state_map.pending = self._pending or "---"
        self.state_map.slippage = self._slip.format_report()
        self.state_map.perf = self._perf.format_short()
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ══════════════════════════════════════════════════════════════════════
    #  执行 (LONG: open=buy, close=sell) — 止损立即执行, 不走VWAP
    # ══════════════════════════════════════════════════════════════════════

    def _resubmit_escalated(self, old_oid, next_urgency: str, info: dict) -> None:
        """撤掉未成交订单, 按 next_urgency 重新挂单.

        策略: 完全复用原订单的方向/手数/kind, 只换 urgency→新价格。
        """
        direction = info.get("direction")
        kind = info.get("kind")
        vol = info.get("vol", 0)
        if not direction or not kind or vol <= 0:
            return
        if self._pricer is None or self._pricer.last == 0:
            return
        p = self.params_map

        # 撤老单
        self.cancel_order(old_oid)
        self._om.on_cancel(old_oid)
        self.order_id.discard(old_oid)

        # 按新 urgency 计算新价
        new_price = self._pricer.price(direction, next_urgency)

        # 重新发单 (open 用 send_order, close/reduce 用 auto_close_position)
        if kind == "open":
            new_oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=new_price, order_direction=direction,
            )
        else:
            new_oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=new_price, order_direction=direction,
            )
        if new_oid is None:
            self.output(f"[ESCALATE] oid={old_oid} → {next_urgency}: 重发 None")
            return
        self.order_id.add(new_oid)
        self._om.on_send(new_oid, vol, new_price,
                         urgency=next_urgency, direction=direction, kind=kind)
        self.output(
            f"[ESCALATE] {old_oid} → {new_oid} | {direction} {vol}手 "
            f"@ {new_price:.1f} | {info.get('urgency')} → {next_urgency}"
        )

    def _aggressive_price(self, price, direction, urgency: str = "normal"):
        """Spread-aware 限价定价.

        price 作为 fallback (pricer 未初始化 / 无 book 且 last=0)。
        urgency: passive / normal / cross / urgent / critical
          - passive  OPEN/ADD 建仓 (escalator 会升级)
          - normal   REDUCE 减仓 + 信号 CLOSE
          - cross    VWAP 分批
          - urgent   HARD_STOP / TRAIL_STOP
          - critical EQUITY_STOP / CIRCUIT / DAILY_STOP / FLATTEN
        """
        if self._pricer is None or self._pricer.last == 0:
            return price
        return self._pricer.price(direction, urgency)

    def _execute(self, kline: KLineData, action: str) -> float:
        price = kline.close
        p = self.params_map
        # 非交易时段防御: 立即执行动作也不能在非交易时段发单
        if self._guard is not None and not self._guard.should_trade():
            self.output(f"[执行跳过] 非交易时段, 延后 {action}")
            return 0.0
        actual = self.get_position(p.instrument_id).net_position

        if action == "OPEN":
            target = self._pending_target or 1
            vol = max(1, target)
            acct = self._get_account()
            if acct and price * self._multiplier * vol * 0.15 > acct.available * 0.6:
                self.output("[保证金不足]")
                feishu("error", p.instrument_id, f"**保证金不足** 需开{vol}手")
                return 0.0
            self._slip.set_signal_price(price)
            buy_price = self._aggressive_price(price, "buy", urgency="passive")
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=buy_price, order_direction="buy",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, buy_price,
                                 urgency="passive", direction="buy", kind="open")
            self.avg_price = price
            self.peak_price = price
            self.state_map.last_action = f"建仓{vol}手"
            self._rec("建仓", vol, "买", price, actual, actual + vol)
            feishu("open", p.instrument_id,
                   f"**建仓** {vol}手 @ {price:,.1f}\n"
                   f"逻辑: {self._pending_reason}\n"
                   f"持仓: {actual} -> {actual + vol}手")
            self._save()
            return price

        elif action == "ADD":
            target = self._pending_target or (actual + 1)
            vol = max(1, target - actual)
            acct = self._get_account()
            if acct and price * self._multiplier * vol * 0.15 > acct.available * 0.6:
                self.output("[加仓保证金不足]")
                return 0.0
            self._slip.set_signal_price(price)
            buy_price = self._aggressive_price(price, "buy", urgency="passive")
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=buy_price, order_direction="buy",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, buy_price,
                                 urgency="passive", direction="buy", kind="open")
            self.avg_price = (
                (self.avg_price * actual + price * vol) / (actual + vol)
                if actual > 0 else price
            )
            self.state_map.last_action = f"加仓{vol}手"
            self._rec("加仓", vol, "买", price, actual, actual + vol)
            feishu("add", p.instrument_id,
                   f"**加仓** {vol}手 @ {price:,.1f}\n"
                   f"逻辑: {self._pending_reason}\n"
                   f"均价: {self.avg_price:.1f}\n"
                   f"持仓: {actual} -> {actual + vol}手")
            self._save()
            return price

        elif action == "REDUCE":
            vol = max(1, actual // 2)
            if actual <= 0:
                return 0.0
            self._slip.set_signal_price(price)
            sell_price = self._aggressive_price(price, "sell", urgency="normal")
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=sell_price, order_direction="sell",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, sell_price,
                                 urgency="normal", direction="sell", kind="reduce")
            self.state_map.last_action = f"减仓{vol}手"
            self._rec("减仓", vol, "卖", price, actual, actual - vol)
            feishu("reduce", p.instrument_id,
                   f"**减仓** {vol}手 @ {price:,.1f}\n"
                   f"逻辑: {self._pending_reason}\n"
                   f"持仓: {actual} -> {actual - vol}手")
            self._save()
            return -price

        elif action in ("CLOSE", "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP",
                         "CIRCUIT", "DAILY_STOP", "FLATTEN"):
            return self._exec_close(kline, actual, action)

        return 0.0

    def _exec_stop_at_tick(self, price: float, action: str, reason: str) -> None:
        """Tick 触发的止损立即执行 (绕过 VWAP).

        price 来自 tick.last_price,动作来自 check_hard_stop_tick /
        check_trail_minutely。执行路径与 _exec_close 一致,但用 tick 价而非
        bar close 价,并同步清理 VWAP/挂单/pending。
        """
        p = self.params_map
        if self._guard is not None and not self._guard.should_trade():
            return
        pos = self.get_position(p.instrument_id)
        if pos is None:
            return
        actual = pos.net_position
        if actual <= 0:
            return

        if self._vwap_active:
            self._vwap_cancel()
        for oid in list(self.order_id):
            self.cancel_order(oid)
            if self._entry is not None:
                self._entry.register_cancelled(oid)

        self._pending_reason = reason
        self._slip.set_signal_price(price)
        # 止损路径 urgency: HARD/TRAIL → urgent, EQUITY/CIRCUIT/DAILY/FLATTEN → critical
        stop_urgency = "critical" if action in ("EQUITY_STOP", "CIRCUIT", "DAILY_STOP", "FLATTEN") else "urgent"
        sell_price = self._aggressive_price(price, "sell", urgency=stop_urgency)
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=actual, price=sell_price, order_direction="sell",
        )
        if oid is None:
            self.output(f"[TICK_STOP] auto_close_position 返回 None, 保留 _pending={action} 等 bar 重试")
            feishu("error", p.instrument_id,
                   f"**止损发单失败** action={action}\n逻辑: {reason}\n等待 bar 级重试")
            return
        self.order_id.add(oid)
        self._om.on_send(oid, actual, sell_price,
                         urgency=stop_urgency, direction="sell", kind="close")

        labels = {
            "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损",
            "EQUITY_STOP": "权益止损", "CIRCUIT": "熔断",
            "DAILY_STOP": "单日止损", "FLATTEN": "即将收盘清仓",
            "CLOSE": "信号平仓",
        }
        label = labels.get(action, action)
        pnl_pct = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
        abs_pnl = self._perf.on_close(self.avg_price, price, actual)
        self.state_map.last_action = f"{label}[TICK] {pnl_pct:+.2f}%"
        self._rec(label, actual, "卖", price, actual, 0)
        feishu(action.lower(), p.instrument_id,
               f"**{label}** (tick触发) {actual}手 @ {price:,.1f}\n"
               f"逻辑: {reason}\n"
               f"盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})\n"
               f"持仓: {actual} -> 0手")
        self.avg_price = 0.0
        self.peak_price = 0.0
        # 保留 self._pending=action 阻止同一分钟/下一分钟的 tick 重复触发;
        # bar-level safety net 会在下一 bar close 清理 (见 _on_bar 开头)
        # 同步清理 risk 内部极值, 避免 trail line 残留
        self._risk.peak_price = 0.0
        self._risk.trough_price = 0.0
        self._risk._last_trail_minute = None
        self._pending_target = None
        self._save()

    def _exec_close(self, kline: KLineData, actual: int, action: str) -> float:
        """统一平仓逻辑 (多头: 卖出平仓)."""
        labels = {
            "CLOSE": "信号平仓", "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损",
            "EQUITY_STOP": "权益止损", "CIRCUIT": "熔断", "DAILY_STOP": "单日止损",
            "FLATTEN": "即将收盘清仓",
        }
        label = labels.get(action, action)
        p = self.params_map
        price = kline.close

        if actual <= 0:
            return 0.0
        self._slip.set_signal_price(price)
        # bar 级平仓 urgency 随 action:止损类 urgent,信号 CLOSE normal
        bar_urgency = "normal" if action == "CLOSE" else (
            "critical" if action in ("EQUITY_STOP", "CIRCUIT", "DAILY_STOP", "FLATTEN") else "urgent"
        )
        sell_price = self._aggressive_price(price, "sell", urgency=bar_urgency)
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=actual, price=sell_price, order_direction="sell",
        )
        if oid is not None:
            self.order_id.add(oid)
            self._om.on_send(oid, actual, sell_price,
                             urgency=bar_urgency, direction="sell", kind="close")
        pnl_pct = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
        abs_pnl = self._perf.on_close(self.avg_price, price, actual)
        self.state_map.last_action = f"{label} {pnl_pct:+.2f}%"
        self._rec(label, actual, "卖", price, actual, 0)
        feishu(action.lower(), p.instrument_id,
               f"**{label}** {actual}手 @ {price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})\n"
               f"持仓: {actual} -> 0手")
        self.avg_price = 0.0
        self.peak_price = 0.0
        self._save()
        return -price

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
            "peak_price": self.peak_price,
            "avg_price": self.avg_price,
            "signal": self.state_map.signal,
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
        available = acct.available if acct else 0
        pos_profit = acct.position_profit if acct else 0
        start_eq = self._risk.daily_start_eq
        daily_abs = eq - start_eq
        daily_pct = self._risk.daily_pnl_pct * 100
        dd_pct = self._risk.drawdown_pct * 100
        peak_eq = self._risk.peak_equity

        account_info = (
            f"**📊 账户概览**\n"
            f"日初权益: {start_eq:,.0f}\n"
            f"当前权益: {eq:,.0f}\n"
            f"可用资金: {available:,.0f}\n"
            f"日盈亏: {daily_abs:+,.0f} ({daily_pct:+.2f}%)\n"
            f"峰值权益: {peak_eq:,.0f} | 回撤: {dd_pct:.2f}%"
        )

        if net > 0:
            position_info = (
                f"\n\n**📋 持仓明细**\n"
                f"合约: {p.instrument_id} | 方向: 多 | 手数: {net}\n"
                f"均价: {self.avg_price:.1f} | 峰值: {self.peak_price:.1f}\n"
                f"浮盈: {pos_profit:+,.0f}"
            )
        else:
            position_info = "\n\n**📋 持仓明细**\n无持仓"

        if self._today_trades:
            trade_info = f"\n\n**📝 今日交易 ({len(self._today_trades)}笔)**\n"
            trade_info += "| 时间 | 操作 | 手数 | 价格 | 持仓变化 |\n|--|--|--|--|--|\n"
            for t in self._today_trades[-20:]:
                trade_info += (f"| {t['time']} | {t['action']} | "
                               f"{t['lots']}({t['side']}) | {t['price']} | "
                               f"{t['before']}->{t['after']} |\n")
        else:
            trade_info = "\n\n**📝 今日交易**\n无交易"

        perf_info = f"\n\n**📈 绩效统计**\n{self._perf.format_report(p.instrument_id)}\n{self._slip.format_report()}"

        feishu("daily_review", p.instrument_id,
               f"**{STRATEGY_NAME} 每日总结**\n"
               f"交易日: {self._current_td}\n\n"
               f"{account_info}{position_info}{trade_info}{perf_info}")

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
        self.order_id.discard(trade.order_id)
        self._om.on_fill(trade.order_id)

        direction = "buy" if "买" in str(trade.direction) else "sell"
        slip = self._slip.on_fill(trade.price, trade.volume, direction)
        if slip != 0:
            self.output(f"[滑点] {slip:.1f}ticks")

        # Scaled entry 通知成交 (2026-04-17)
        if self._entry is not None:
            self._entry.on_trade(trade.order_id, trade.price, trade.volume, datetime.now())

        # VWAP成交回报 (legacy, 保留)
        if self._vwap_active:
            self._vwap_fill_pv += trade.price * trade.volume
            self._vwap_fill_lots += trade.volume
            if self._vwap_remaining == 0:
                self._vwap_complete()

        # 用实际成交价更新avg_price (多头)
        pos = self.get_position(self.params_map.instrument_id)
        actual = pos.net_position if pos else 0
        if direction == "buy" and actual > 0:
            old_pos = max(0, actual - trade.volume)
            if old_pos > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old_pos + trade.price * trade.volume) / actual
            else:
                self.avg_price = trade.price
            if trade.price > self.peak_price or self.peak_price == 0:
                self.peak_price = trade.price
        elif direction == "sell" and actual <= 0:
            self.avg_price = 0.0
            self.peak_price = 0.0

        self.state_map.net_pos = actual
        self._save()
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)
        self._om.on_cancel(order.order_id)
        # VWAP取消回调: 被撤的量加回remaining
        if self._vwap_active and order.volume > 0:
            if self._vwap_direction == "buy":
                self._vwap_remaining += order.volume
            else:
                self._vwap_remaining -= order.volume

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
