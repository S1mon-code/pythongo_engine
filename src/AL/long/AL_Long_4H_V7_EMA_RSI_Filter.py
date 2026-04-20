"""
================================================================================
  AL_Long_4H_V7 — EMA Trend + RSI Filter (电解铝 H4做多)
================================================================================

  QBase_v3 策略: i_long_v7_ema_rsi_filter
  信号: EMA趋势确认 + RSI甜蜜区[40, 70]钟形曲线 → [0, 1]
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

# ── 模块导入 (和TestFullModule完全一致) ──
from modules.contract_info import get_multiplier, get_tick_size
from modules.error_handler import throttle_on_error
from modules.session_guard import SessionGuard
from modules.feishu import feishu
from modules.persistence import save_state, load_state
from modules.trading_day import get_trading_day, is_new_day, DAY_START_HOUR
from modules.risk import check_stops, RiskManager
from modules.slippage import SlippageTracker
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.rollover import check_rollover
from modules.position_sizing import calc_optimal_lots, apply_buffer
from modules.twap import TWAPExecutor, IMMEDIATE_ACTIONS


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "AL_Long_4H_V7"

# 策略指标参数 (QBase_v3 默认值)
EMA_PERIOD = 60
RSI_PERIOD = 14
RSI_FLOOR = 40.0
RSI_CEILING = 70.0
WARMUP = 100

# Chandelier Exit
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 2.5

# Vol Targeting
FORECAST_SCALAR = 10.0
FORECAST_CAP = 20.0
ANNUAL_FACTOR = 252 * 3            # H4: AL有夜盘, ~3 bars/day

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


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL — EMA trend + RSI bell-curve filter
# ══════════════════════════════════════════════════════════════════════════════

def generate_signal(closes, bar_idx):
    """EMA趋势 + RSI甜蜜区钟形信号 → [0, 1]. Long only."""
    if bar_idx < WARMUP:
        return 0.0

    ema_arr = _ema(closes, EMA_PERIOD)
    rsi_arr = _rsi(closes, RSI_PERIOD)

    close = closes[bar_idx]
    e = ema_arr[bar_idx]
    r = rsi_arr[bar_idx]

    if np.isnan(e) or np.isnan(r):
        return 0.0

    # Gate: price must be above EMA
    if close <= e:
        return 0.0

    # Gate: RSI must be in sweet spot
    if r < RSI_FLOOR or r > RSI_CEILING:
        return 0.0

    # Bell-curve weighting within RSI range
    mid = (RSI_FLOOR + RSI_CEILING) / 2.0
    if r <= mid:
        return float((r - RSI_FLOOR) / (mid - RSI_FLOOR))
    return float((RSI_CEILING - r) / (RSI_CEILING - mid))


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
    kline_style: str = Field(default="H4", title="K线周期")
    max_lots: int = Field(default=5, title="最大持仓")
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


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

class AL_Long_4H_V7_EMA_RSI_Filter(BaseStrategy):
    """电解铝 H4做多 — EMA Trend + RSI Filter"""

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
        self._twap = TWAPExecutor()
        self._perf = None
        self._multiplier = 5

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
            self._current_td = saved.get("trading_day", "")
            self._today_trades = saved.get("today_trades", [])
            self.output(f"[恢复] peak_eq={self._risk.peak_equity:.0f} avg={self.avg_price:.1f}")

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
               f"乘数: {self._multiplier}\n持仓: {actual}手")

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
        # 第一层: K线数据 (永远不能断)
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)

        try:
            self._on_tick_stops(tick)
        except Exception as e:
            self.output(f"[stops异常] {type(e).__name__}: {e}")

        # 第二层: TWAP执行
        try:
            self._on_tick_twap(tick)
        except Exception as e:
            self.output(f"[TWAP异常] {type(e).__name__}: {e}")

        # 第三层: 辅助逻辑
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
            return

        # 移动止损 — 每分钟
        action, reason = self._risk.check_trail_minutely(
            price=price, now=datetime.now(),
            net_pos=net_pos, trailing_pct=p.trailing_pct,
        )
        if action:
            self.output(f"[{action}][M1] {reason}")
            self._exec_stop_at_tick(price, action, reason)

    def _on_tick_twap(self, tick: TickData):
        """TWAP分批执行."""
        if not self._twap.is_active:
            return
        batch = self._twap.check()
        if batch is None:
            return
        p = self.params_map
        price = tick.last_price
        direction = self._twap.direction
        agg_price = self._aggressive_price(price, direction)
        if direction == "buy":
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=batch, price=agg_price, order_direction="buy",
            )
        else:
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=batch, price=agg_price, order_direction="sell",
            )
        if oid is not None:
            self.order_id.add(oid)
            self._om.on_send(oid, batch, price)
            self._twap.on_send(oid, batch)
        self.output(f"[TWAP] {direction} {batch}手 @ {price:.1f} ({self._twap.progress})")

    def _on_tick_aux(self, tick: TickData):
        p = self.params_map

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

        # 撤挂单 (TWAP进行中不撤)
        if not self._twap.is_active:
            for oid in list(self.order_id):
                self.cancel_order(oid)
            for oid in self._om.check_timeouts(self.cancel_order):
                self.output(f"[超时撤单] {oid}")

        # 执行pending: 止损立即(即使TWAP进行中也要执行), 正常信号→TWAP
        if self._pending is not None:
            action = self._pending
            if action in IMMEDIATE_ACTIONS:
                # 止损优先: 取消TWAP, 立即执行
                if self._twap.is_active:
                    self._twap.cancel()
                    for oid in list(self.order_id):
                        self.cancel_order(oid)
                    self.output(f"[TWAP取消+撤单] 止损优先: {action}")
                signal_price = self._execute(kline, action)
            elif self._twap.is_active:
                # TWAP进行中, 非止损pending忽略
                self.output(f"[TWAP进行中] 忽略pending {action}")
                signal_price = 0.0
            else:
                self._submit_twap(kline, action)
                signal_price = 0.0
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # TWAP进行中 → 不产生新信号, 但仍需检查止损
        if self._twap.is_active:
            self.output(f"[TWAP进行中] {self._twap.progress}")
            # 继续往下走, 让止损检查能执行
            # 但不产生新的正常信号

        # 数据准备
        producer = self.kline_generator.producer
        if len(producer.close) < WARMUP + 2:
            self._push_widget(kline, signal_price)
            return

        closes = np.array(producer.close, dtype=np.float64)
        highs = np.array(producer.high, dtype=np.float64)
        lows = np.array(producer.low, dtype=np.float64)
        bar_idx = len(closes) - 1
        close = float(closes[-1])

        # 指标调试输出
        ema_arr = _ema(closes, EMA_PERIOD)
        rsi_arr = _rsi(closes, RSI_PERIOD)
        e_val = ema_arr[bar_idx] if not np.isnan(ema_arr[bar_idx]) else 0.0
        r_val = rsi_arr[bar_idx] if not np.isnan(rsi_arr[bar_idx]) else 0.0
        self.output(f"[IND] EMA{EMA_PERIOD}={e_val:.1f} RSI{RSI_PERIOD}={r_val:.1f} close={close:.1f}")

        # 信号计算
        raw = generate_signal(closes, bar_idx)
        forecast = min(FORECAST_CAP, max(0.0, raw * FORECAST_SCALAR))
        self.state_map.signal = round(raw, 3)
        self.state_map.forecast = round(forecast, 1)
        self.output(f"[SIGNAL] raw={raw:.4f} forecast={forecast:.1f}")

        # 仓位计算 (四舍五入到整数手)
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

        # 持仓追踪 (peak 由 _on_tick_stops 维护,此处只同步显示)
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

        # 权益
        acct = self._get_account()
        equity = pos_profit = 0.0
        if acct:
            equity = acct.balance
            pos_profit = acct.position_profit
            self._risk.update(equity)
            self.state_map.equity = round(equity, 0)
            self.state_map.drawdown = f"{self._risk.drawdown_pct:.2%}"
            self.state_map.daily_pnl = f"{self._risk.daily_pnl_pct:+.2%}"

        # 盘前清仓已禁用 — 完全靠信号和止损管理
        # # ── 盘前清仓 ──
        # if self._guard.should_flatten() and net_pos > 0:
        #     self._pending_reason = f"距收盘<{p.flatten_minutes}分钟, 自动清仓"
        #     self._exec_close(kline, net_pos, "FLATTEN")
        #     self._push_widget(kline, -kline.close)
        #     self.update_status_bar()
        #     return

        # ── 止损检查 (多头: RiskManager直接处理) ──
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

        # ── 正常信号 → pending (TWAP进行中不产生新正常信号) ──
        if self._pending is None and not self._twap.is_active and target != net_pos:
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
                if self._twap.is_active:
                    self._twap.cancel()
                    for oid in list(self.order_id):
                        self.cancel_order(oid)
                    self.output(f"[TWAP取消+撤单] 止损优先: {action}")
                signal_price = self._execute(kline, action)
            elif not self._twap.is_active:
                self._submit_twap(kline, action)
            else:
                self.output(f"[TWAP进行中] 忽略pending {action}")
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""

        self.state_map.pending = self._pending or "---"
        self.state_map.slippage = self._slip.format_report()
        self.state_map.perf = self._perf.format_short()
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ══════════════════════════════════════════════════════════════════════
    #  执行 (LONG: open=buy, close=sell)
    # ══════════════════════════════════════════════════════════════════════

    def _aggressive_price(self, price, direction):
        """SHFE实盘不支持市价卖单，用当前价限价单代替."""
        return price

    def _submit_twap(self, kline: KLineData, action: str):
        """将正常信号提交给TWAP分批执行."""
        p = self.params_map
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
                self.output("[保证金不足] TWAP取消")
                feishu("error", p.instrument_id, f"**保证金不足** TWAP {action} {vol}手")
                return

        self._twap.submit(action, vol, direction, self._pending_reason, p.instrument_id)
        self.output(f"[TWAP提交] {action} {vol}手 {direction}")
        feishu("info", p.instrument_id,
               f"**TWAP启动** {action}\n目标: {vol}手 {direction}\n窗口: 第2-11分钟\n逻辑: {self._pending_reason}")

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
            buy_price = self._aggressive_price(price, "buy")
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=buy_price, order_direction="buy",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
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
            buy_price = self._aggressive_price(price, "buy")
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=buy_price, order_direction="buy",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
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
            sell_price = self._aggressive_price(price, "sell")
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=sell_price, order_direction="sell",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
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
        """Tick 触发的止损立即执行 (绕过 VWAP/TWAP).

        price 来自 tick.last_price,动作来自 check_hard_stop_tick /
        check_trail_minutely。执行路径与 _exec_close 一致,但用 tick 价而非
        bar close 价,并同步清理 VWAP/TWAP/挂单/pending。
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

        if hasattr(self, '_vwap_active') and self._vwap_active:
            self._vwap_cancel()
        if hasattr(self, '_twap') and self._twap.is_active:
            self._twap.cancel()
        for oid in list(self.order_id):
            self.cancel_order(oid)

        self._pending_reason = reason
        self._slip.set_signal_price(price)
        sell_price = self._aggressive_price(price, "sell")
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
        self._om.on_send(oid, actual, price)

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
        sell_price = self._aggressive_price(price, "sell")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=actual, price=sell_price, order_direction="sell",
        )
        if oid is not None:
            self.order_id.add(oid)
            self._om.on_send(oid, actual, price)
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

        # 账户信息
        account_info = (
            f"**📊 账户概览**\n"
            f"日初权益: {start_eq:,.0f}\n"
            f"当前权益: {eq:,.0f}\n"
            f"可用资金: {available:,.0f}\n"
            f"日盈亏: {daily_abs:+,.0f} ({daily_pct:+.2f}%)\n"
            f"峰值权益: {peak_eq:,.0f} | 回撤: {dd_pct:.2f}%"
        )

        # 持仓明细
        if net > 0:
            position_info = (
                f"\n\n**📋 持仓明细**\n"
                f"合约: {p.instrument_id} | 方向: 多 | 手数: {net}\n"
                f"均价: {self.avg_price:.1f} | 峰值: {self.peak_price:.1f}\n"
                f"浮盈: {pos_profit:+,.0f}"
            )
        else:
            position_info = "\n\n**📋 持仓明细**\n无持仓"

        # 今日交易
        if self._today_trades:
            trade_info = f"\n\n**📝 今日交易 ({len(self._today_trades)}笔)**\n"
            trade_info += "| 时间 | 操作 | 手数 | 价格 | 持仓变化 |\n|--|--|--|--|--|\n"
            for t in self._today_trades[-20:]:
                trade_info += (f"| {t['time']} | {t['action']} | "
                               f"{t['lots']}({t['side']}) | {t['price']} | "
                               f"{t['before']}->{t['after']} |\n")
        else:
            trade_info = "\n\n**📝 今日交易**\n无交易"

        # 绩效
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

        direction = ("buy" if str(trade.direction).lower() in ("buy", "0", "买") else "sell")
        slip = self._slip.on_fill(trade.price, trade.volume, direction)
        if slip != 0:
            self.output(f"[滑点] {slip:.1f}ticks")

        # TWAP成交回报
        if self._twap.is_active:
            self._twap.on_fill(trade.volume, trade.price)
            if not self._twap.is_active:
                feishu("info", self.params_map.instrument_id,
                       f"**TWAP完成** {self._twap.action}\n成交: {self._twap.progress} VWAP={self._twap.vwap:.1f}")

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
        self._twap.on_cancel(order.order_id, order.cancel_volume)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
        throttle_on_error(self, error)
