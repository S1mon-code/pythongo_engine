"""LC_Short_4H_V14 — OI Flow + EMA 做空 (碳酸锂4H)

QBase_v3 策略: lc_short_v14_oi_flow_ema
信号逻辑:
  OI Flow看空 + 价格低于EMA → [-1, 0]
  空头信号: OI flow低于信号线 + 持续走弱
止损: Chandelier Exit (Short) + 内联短止损
仓位: Vol Targeting + Carver 10% buffer
部署: modules/ → pyStrategy/modules/, 本文件 → self_strategy/
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
from modules.trading_day import get_trading_day, is_new_day, DAY_START_HOUR
from modules.risk import check_stops, RiskManager
from modules.slippage import SlippageTracker
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.twap import TWAPExecutor, IMMEDIATE_ACTIONS
from modules.performance import PerformanceTracker
from modules.rollover import check_rollover
from modules.position_sizing import calc_optimal_lots, apply_buffer


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "LC_Short_4H_V14"

# 策略指标参数
EMA_PERIOD = 50
OI_PERIOD = 14
FLOW_LOOKBACK = 10
SIGNAL_STRENGTH = 0.8
WARMUP = 55

# Chandelier Exit
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 3.0

# Vol Targeting
FORECAST_SCALAR = 10.0
FORECAST_CAP = 20.0
ANNUAL_FACTOR = 252 * 1          # H4: LC无夜盘, ~1根H4/天

# 日报时间
DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def _ema(arr, period):
    """EMA with SMA seed."""
    n = len(arr)
    out = np.full(n, np.nan)
    if n < period:
        return out
    out[period - 1] = np.nanmean(arr[:period])
    k = 2.0 / (period + 1)
    for i in range(period, n):
        if np.isnan(out[i - 1]) or np.isnan(arr[i]):
            out[i] = out[i - 1] if not np.isnan(out[i - 1]) else arr[i]
        else:
            out[i] = arr[i] * k + out[i - 1] * (1 - k)
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


def _oi_flow(closes, oi, volumes, period=14):
    """OI Flow — rate of OI change normalized by volume."""
    n = len(closes)
    flow = np.full(n, np.nan)
    flow_sig = np.full(n, np.nan)
    if n < period + 1:
        return flow, flow_sig
    for i in range(1, n):
        if volumes[i] > 0:
            flow[i] = (oi[i] - oi[i - 1]) / (volumes[i] + 1e-10)
        else:
            flow[i] = 0.0
    flow_sig = _ema(flow, period)
    return flow, flow_sig


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL — OI Flow bearish + below EMA → short
# ══════════════════════════════════════════════════════════════════════════════

def generate_signal(closes, oi, volumes, bar_idx):
    """OI Flow + EMA 做空信号 → [-1, 0]."""
    if bar_idx < WARMUP:
        return 0.0

    ema_arr = _ema(closes, EMA_PERIOD)
    flow, flow_sig = _oi_flow(closes, oi, volumes, OI_PERIOD)

    close = closes[bar_idx]
    e = ema_arr[bar_idx]
    fl = flow[bar_idx]
    fs = flow_sig[bar_idx]

    if np.isnan(e) or np.isnan(fl) or np.isnan(fs):
        return 0.0

    # Must be below EMA for short
    if close > e:
        return 0.0

    # OI flow below signal = bearish
    if fl < fs:
        lb_idx = max(0, bar_idx - FLOW_LOOKBACK)
        fl_start = flow[lb_idx]
        if not np.isnan(fl_start) and fl < fl_start:
            return -SIGNAL_STRENGTH
        return -SIGNAL_STRENGTH * 0.5

    return 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  CHANDELIER EXIT (SHORT)
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
    exchange: str = Field(default="GFEX", title="交易所")
    instrument_id: str = Field(default="lc2609", title="合约")
    kline_style: str = Field(default="H4", title="K线")
    max_lots: int = Field(default=5, title="最大手数")
    capital: float = Field(default=1_000_000, title="资金")
    hard_stop_pct: float = Field(default=0.5, title="硬止损%")
    trailing_pct: float = Field(default=0.3, title="移动止损%")
    equity_stop_pct: float = Field(default=2.0, title="权益止损%")
    flatten_minutes: int = Field(default=5, title="即将收盘提示(分钟)")
    sim_24h: bool = Field(default=False, title="模拟盘")


class State(BaseState):
    signal: float = Field(default=0.0, title="信号")
    forecast: float = Field(default=0.0, title="预测")
    target_lots: int = Field(default=0, title="目标手")
    net_pos: int = Field(default=0, title="持仓")
    avg_price: float = Field(default=0.0, title="均价")
    trough_price: float = Field(default=0.0, title="谷价")
    hard_line: float = Field(default=0.0, title="止损线")
    trail_line: float = Field(default=0.0, title="移损线")
    equity: float = Field(default=0.0, title="权益")
    drawdown: str = Field(default="---", title="回撤")
    daily_pnl: str = Field(default="---", title="日PnL")
    trading_day: str = Field(default="", title="交易日")
    session: str = Field(default="---", title="时段")
    pending: str = Field(default="---", title="待执行")
    last_action: str = Field(default="---", title="操作")
    slippage: str = Field(default="---", title="滑点")
    perf: str = Field(default="---", title="绩效")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

class LC_Short_4H_V14_OI_Flow_EMA(BaseStrategy):
    """碳酸锂4H做空 — OI Flow + EMA"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        # Next-bar pending
        self._pending = None
        self._pending_target = None
        self._pending_reason = ""
        self.order_id = set()

        # 持仓
        self.avg_price = 0.0
        self.trough_price = 0.0

        # OI数据收集
        self._oi_data = []

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
        self._twap = TWAPExecutor()
        self._perf = None
        self._multiplier = 1

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

        # 合约参数
        self._multiplier = get_multiplier(p.instrument_id)

        # 初始化需要instrument_id的模块
        self._guard = SessionGuard(p.instrument_id, p.flatten_minutes, sim_24h=p.sim_24h)
        self._slip = SlippageTracker(p.instrument_id)
        self._hb = HeartbeatMonitor(p.instrument_id)
        self._perf = PerformanceTracker(p.instrument_id)

        # K线
        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self.kline_generator.push_history_data()

        # 账户ID
        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id

        # 风控管理器
        self._risk = RiskManager(capital=p.capital)

        # 恢复状态
        saved = load_state(STRATEGY_NAME)
        if saved:
            self._risk.load_state(saved)
            self.trough_price = saved.get("trough_price", 0.0)
            self.avg_price = saved.get("avg_price", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._today_trades = saved.get("today_trades", [])
            self.output(
                f"[恢复] peak_eq={self._risk.peak_equity:.0f} "
                f"avg={self.avg_price:.1f} trough={self.trough_price:.1f}"
            )

        # 权益初始化
        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance, acct.position_profit)

        # 信任broker持仓
        pos = self.get_position(p.instrument_id)
        actual = abs(pos.net_position) if pos else 0
        self.state_map.net_pos = -actual
        if actual == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        # 换月检查
        level, days = check_rollover(p.instrument_id)
        if level:
            feishu("rollover", p.instrument_id, f"**换月**: 距交割月{days}天")

        super().on_start()
        self.output(f"{STRATEGY_NAME} 启动 | {p.instrument_id} {p.kline_style} | 持仓={actual}")
        feishu("start", p.instrument_id,
               f"**策略启动** {STRATEGY_NAME}\n合约: {p.instrument_id}\n持仓: {actual}手")

    def on_stop(self):
        self._save()
        feishu("shutdown", self.params_map.instrument_id,
               f"**停止** {STRATEGY_NAME}\n持仓: {self.state_map.net_pos}手\n"
               f"{self._slip.format_report()}")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        # 第一层: K线数据 (永远不能断)
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)

        # 第二层: Tick级止损 (优先于TWAP, 2026-04-17 重构)
        try:
            self._on_tick_stops(tick)
        except Exception as e:
            self.output(f"[stops异常] {type(e).__name__}: {e}")

        # 第三层: TWAP执行 (不能被辅助逻辑异常中断)
        try:
            self._on_tick_twap(tick)
        except Exception as e:
            self.output(f"[TWAP异常] {type(e).__name__}: {e}")

        # 第四层: 辅助逻辑 (异常不影响K线和TWAP)
        try:
            self._on_tick_aux(tick)
        except Exception as e:
            self.output(f"[on_tick异常] {type(e).__name__}: {e}")
            feishu("error", self.params_map.instrument_id,
                   f"**on_tick异常**\n{type(e).__name__}: {e}")

    def _on_tick_stops(self, tick: TickData):
        """Tick 级止损检查 — 做空方向 (2026-04-17 重构)."""
        if not self.trading:
            return
        if self._guard is not None and not self._guard.should_trade():
            return
        if self._pending is not None:
            return
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        if pos is None:
            return
        raw_pos = pos.net_position
        price = tick.last_price

        self._risk.update_peak_trough_tick(price, raw_pos)
        self.trough_price = self._risk.trough_price

        if raw_pos >= 0:
            return

        action, reason = self._risk.check_hard_stop_tick(
            price=price, avg_price=self.avg_price,
            net_pos=raw_pos, hard_stop_pct=p.hard_stop_pct,
        )
        if action:
            self.output(f"[{action}][TICK] {reason}")
            self._exec_stop_at_tick(price, action, reason)
            return

        action, reason = self._risk.check_trail_minutely(
            price=price, now=datetime.now(),
            net_pos=raw_pos, trailing_pct=p.trailing_pct,
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
        if direction == "sell":
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=batch, price=agg_price, order_direction="sell",
            )
        else:
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=batch, price=agg_price, order_direction="buy",
            )
        if oid is not None:
            self.order_id.add(oid)
            self._om.on_send(oid, batch, price)
            self._twap.on_send(oid, batch)
        self.output(f"[TWAP] {direction} {batch}手 @ {price:.1f} ({self._twap.progress})")

    def _on_tick_aux(self, tick: TickData):
        """辅助逻辑: 交易日切换/换月/心跳/日报."""
        p = self.params_map

        # 交易日切换
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
            self.output(f"[新交易日] {td}")

        if not self._current_td:
            self._current_td = td
            self.state_map.trading_day = td

        # 换月 (每天一次)
        if not self._rollover_checked:
            level, days = check_rollover(p.instrument_id)
            if level:
                feishu("rollover", p.instrument_id, f"**换月**: 距交割月{days}天")
            self._rollover_checked = True

        # 心跳
        for atype, msg in self._hb.check(p.instrument_id):
            if atype == "no_tick":
                feishu("no_tick", p.instrument_id, msg)

        # 交易时段状态
        self.state_map.session = self._guard.get_status()

        # 每日回顾
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
        p = self.params_map
        signal_price = 0.0

        # 收集OI数据
        self._oi_data.append(kline.open_interest)

        # 撤挂单 (TWAP进行中不撤)
        if not self._twap.is_active:
            for oid in list(self.order_id):
                self.cancel_order(oid)
            for oid in self._om.check_timeouts(self.cancel_order):
                self.output(f"[超时撤单] {oid}")

        # 历史回放阶段: 只推K线到图表, 不交易不算信号
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
        closes = np.array(producer.close, dtype=np.float64)
        highs = np.array(producer.high, dtype=np.float64)
        lows = np.array(producer.low, dtype=np.float64)
        volumes = np.array(producer.volume, dtype=np.float64)
        oi = np.array(self._oi_data, dtype=np.float64)
        bar_idx = len(closes) - 1

        # OI对齐修复
        if len(oi) < WARMUP:
            self._push_widget(kline, signal_price)
            return
        if len(oi) < len(closes):
            offset = len(closes) - len(oi)
            closes = closes[offset:]
            highs = highs[offset:]
            lows = lows[offset:]
            volumes = volumes[offset:]
            bar_idx = len(closes) - 1

        if bar_idx < WARMUP:
            self._push_widget(kline, signal_price)
            return

        close = closes[bar_idx]

        # 信号计算 (short: raw is negative)
        raw = generate_signal(closes, oi, volumes, bar_idx)
        abs_raw = abs(raw)
        forecast = min(FORECAST_CAP, max(0.0, abs_raw * FORECAST_SCALAR))
        self.state_map.signal = round(raw, 3)
        self.state_map.forecast = round(forecast, 1)

        # 指标debug输出
        ema_arr = _ema(closes, EMA_PERIOD)
        flow, flow_sig = _oi_flow(closes, oi, volumes, OI_PERIOD)
        e = ema_arr[bar_idx]
        fl = flow[bar_idx]
        fs = flow_sig[bar_idx]
        self.output(f"[IND] OI_Flow={fl:.4f} FlowSig={fs:.4f} EMA50={e:.1f} close={close:.1f}")
        self.output(f"[SIGNAL] raw={raw:.4f} forecast={forecast:.1f}")

        # 仓位计算
        atr_arr = _atr(highs, lows, closes)
        optimal_raw = calc_optimal_lots(
            forecast, atr_arr[bar_idx], close,
            p.capital, p.max_lots, self._multiplier, ANNUAL_FACTOR,
        )
        pos = self.get_position(p.instrument_id)
        current = abs(pos.net_position) if pos else 0
        target = min(round(optimal_raw), p.max_lots)
        target = apply_buffer(target, current)

        # forecast=0 → 强制退出 (信号消失不走buffer)
        if forecast == 0 and current > 0:
            target = 0

        self.state_map.net_pos = -current

        # 持仓追踪 (trough 由 _on_tick_stops 维护, 此处只同步显示)
        if current == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0
        else:
            # bar close 也参与 trough 更新, 避免 tick 丢失时谷值漂移
            self._risk.update_peak_trough_tick(close, -current)
            self.trough_price = self._risk.trough_price
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.trough_price = round(self.trough_price, 1)

        # 止损线 (short)
        self.state_map.hard_line = (
            round(self.avg_price * (1 + p.hard_stop_pct / 100), 1)
            if current > 0 and self.avg_price > 0 else 0.0
        )
        self.state_map.trail_line = (
            round(self.trough_price * (1 + p.trailing_pct / 100), 1)
            if current > 0 and self.trough_price > 0 else 0.0
        )

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
        #     self._pending_reason = f"距收盘<{p.flatten_minutes}分钟, 自动清仓"
        #     self._push_widget(kline)
        #     return

        # ── 止损检查 (hard/trail 由 _on_tick_stops 处理, bar 级只负责 equity/portfolio/daily) ──
        if current > 0 and self.avg_price > 0:
            # Equity stop: pos_profit based
            if pos_profit < 0 and abs(pos_profit) > p.capital * p.equity_stop_pct / 100:
                self._pending = "EQUITY_STOP"
                self._pending_target = 0
                self._pending_reason = (
                    f"权益止损 loss={pos_profit:.0f} > "
                    f"limit={p.capital * p.equity_stop_pct / 100:.0f}"
                )
                self._push_widget(kline)
                return

            # Portfolio stops (equity based, from RiskManager)
            # Pass 999.0 to skip hard/trail in RiskManager, only use equity_stop
            action, reason = self._risk.check(
                close=close, avg_price=self.avg_price, peak_price=self.avg_price,
                pos_profit=pos_profit, net_pos=current,
                hard_stop_pct=999.0, trailing_pct=999.0,
                equity_stop_pct=999.0,
            )
            if action and action in ("CIRCUIT", "DAILY_STOP"):
                self._pending = action
                self._pending_target = 0
                self._pending_reason = reason
                self._push_widget(kline)
                return
            elif action == "WARNING":
                feishu("warning", p.instrument_id, f"**预警**: {reason}")

        # ── Chandelier Exit (Short) ──
        if current > 0:
            ch_atr = _atr(highs, lows, closes, CHANDELIER_PERIOD)
            if chandelier_short(lows, closes, ch_atr, bar_idx):
                self._pending = "CLOSE"
                self._pending_target = 0
                self._pending_reason = "Chandelier Exit (Short)"
                self._push_widget(kline)
                return

        # ── 正常信号 → pending (TWAP进行中不产生新正常信号) ──
        if not self._twap.is_active and target != current:
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
                f"signal={raw:.2f} forecast={forecast:.1f} "
                f"optimal={optimal_raw:.1f} target={target}"
            )

        self.state_map.target_lots = -target

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
    #  TWAP / 执行 (SHORT: sell to open, buy to close)
    # ══════════════════════════════════════════════════════════════════════

    def _aggressive_price(self, price, direction=None):
        """返回价格不变 (LC: 无需调整)."""
        return price

    def _submit_twap(self, kline: KLineData, action: str):
        """将正常信号提交给TWAP分批执行."""
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        actual = abs(pos.net_position) if pos else 0

        if action == "OPEN":
            vol = max(1, self._pending_target or 1)
            direction = "sell"    # 做空开仓 = 卖
        elif action == "ADD":
            vol = max(1, (self._pending_target or (actual + 1)) - actual)
            direction = "sell"
        elif action == "REDUCE":
            vol = max(1, actual - (self._pending_target or (actual // 2)))
            direction = "buy"     # 做空减仓 = 买
        elif action == "CLOSE":
            vol = actual
            direction = "buy"     # 做空平仓 = 买
        else:
            return

        if vol <= 0:
            return

        if direction == "sell":
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
        pos = self.get_position(p.instrument_id)
        actual = abs(pos.net_position) if pos else 0
        target = self._pending_target if self._pending_target is not None else 0
        reason = self._pending_reason
        diff = target - actual

        if action == "OPEN":
            vol = abs(diff) if diff != 0 else target
            if vol <= 0:
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price, order_direction="sell", market=False,
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.avg_price = price
            self.trough_price = price
            self.state_map.last_action = f"开空{vol}手"
            self._rec("开空", vol, "卖", price, actual, actual + vol)
            feishu("open", p.instrument_id,
                   f"**开空** {vol}手 @ {price:,.1f}\n"
                   f"逻辑: {reason}\n持仓: {actual} → {actual + vol}手")
            self._save()
            return -price

        elif action == "ADD":
            vol = abs(diff) if diff < 0 else 0
            if vol <= 0:
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price, order_direction="sell", market=False,
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.avg_price = (
                (self.avg_price * actual + price * vol) / (actual + vol)
                if actual > 0 else price
            )
            self.state_map.last_action = f"加空{vol}手"
            self._rec("加空", vol, "卖", price, actual, actual + vol)
            feishu("add", p.instrument_id,
                   f"**加空** {vol}手 @ {price:,.1f}\n"
                   f"逻辑: {reason}\n均价: {self.avg_price:.1f}\n"
                   f"持仓: {actual} → {actual + vol}手")
            self._save()
            return -price

        elif action == "REDUCE":
            vol = abs(diff) if diff > 0 else 0
            if vol <= 0 or actual <= 0:
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price, order_direction="buy", market=False,
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.state_map.last_action = f"减仓{vol}手"
            self._rec("减仓", vol, "买", price, actual, actual - vol)
            feishu("reduce", p.instrument_id,
                   f"**减仓** {vol}手 @ {price:,.1f}\n"
                   f"逻辑: {reason}\n持仓: {actual} → {actual - vol}手")
            self._save()
            return price

        elif action in ("CLOSE", "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP",
                         "CIRCUIT", "DAILY_STOP", "FLATTEN"):
            return self._exec_close(kline, actual, action)

        return 0.0

    def _exec_stop_at_tick(self, price: float, action: str, reason: str) -> None:
        """Tick 触发的止损立即执行 — 做空方向 (买入平仓)."""
        p = self.params_map
        if self._guard is not None and not self._guard.should_trade():
            return
        pos = self.get_position(p.instrument_id)
        if pos is None:
            return
        actual = abs(pos.net_position)
        if actual <= 0:
            return

        if hasattr(self, '_twap') and self._twap.is_active:
            self._twap.cancel()
        for oid in list(self.order_id):
            self.cancel_order(oid)

        self._pending_reason = reason
        self._slip.set_signal_price(price)
        buy_price = self._aggressive_price(price, "buy")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=actual, price=buy_price, order_direction="buy",
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
        pnl_pct = (self.avg_price - price) / self.avg_price * 100 if self.avg_price > 0 else 0
        abs_pnl = self._perf.on_close(self.avg_price, price, actual, direction="short")
        self.state_map.last_action = f"{label}[TICK] {pnl_pct:+.2f}%"
        self._rec(label, actual, "买", price, actual, 0)
        feishu(action.lower(), p.instrument_id,
               f"**{label}** (tick触发/空头) {actual}手 @ {price:,.1f}\n"
               f"逻辑: {reason}\n"
               f"盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})\n"
               f"持仓: -{actual} -> 0手")
        self.avg_price = 0.0
        self.trough_price = 0.0
        # 保留 self._pending=action 阻止同一分钟/下一分钟的 tick 重复触发;
        # bar-level safety net 会在下一 bar close 清理 (见 _on_bar 开头)
        # 同步清理 risk 内部极值, 避免 trail line 残留
        self._risk.peak_price = 0.0
        self._risk.trough_price = 0.0
        self._risk._last_trail_minute = None
        self._pending_target = None
        self._save()

    def _exec_close(self, kline: KLineData, actual: int, action: str) -> float:
        """统一平仓逻辑 (short: buy to close)."""
        labels = {
            "CLOSE": "趋势出场", "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损",
            "EQUITY_STOP": "权益止损", "CIRCUIT": "熔断", "DAILY_STOP": "单日止损",
            "FLATTEN": "即将收盘清仓",
        }
        label = labels.get(action, action)
        p = self.params_map
        price = kline.close

        if actual <= 0:
            return 0.0

        self._slip.set_signal_price(price)
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=actual, price=price, order_direction="buy", market=False,
        )
        if oid is not None:
            self.order_id.add(oid)
            self._om.on_send(oid, actual, price)

        # Short PnL: (avg_price - price) / avg_price
        pnl_pct = (
            (self.avg_price - price) / self.avg_price * 100
            if self.avg_price > 0 else 0.0
        )
        abs_pnl = self._perf.on_close(self.avg_price, price, actual, direction="short")
        self.state_map.last_action = f"{label} {pnl_pct:+.2f}%"
        self._rec(label, actual, "买", price, actual, 0)
        feishu(action.lower(), p.instrument_id,
               f"**{label}** {actual}手 @ {price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})\n"
               f"持仓: {actual} → 0手")
        self.avg_price = 0.0
        self.trough_price = 0.0
        self._save()
        return price

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
            "trading_day": self._current_td,
            "today_trades": self._today_trades[-50:],
        }
        state.update(self._risk.get_state() if self._risk is not None else {})
        save_state(state, name=STRATEGY_NAME)

    def _send_review(self):
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        net = abs(pos.net_position) if pos else 0
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
                f"合约: {p.instrument_id} | 方向: 空 | 手数: {net}\n"
                f"均价: {self.avg_price:.1f} | 谷价: {self.trough_price:.1f}\n"
                f"浮盈: {pos_profit:+,.0f}"
            )
        else:
            position_info = "\n\n**📋 持仓明细**\n无持仓"

        # 今日交易
        if self._today_trades:
            trade_info = f"\n\n**📝 今日交易 ({len(self._today_trades)}笔)**\n"
            trade_info += "| 时间 | 操作 | 手数 | 价格 | 持仓变化 |\n|--|--|--|--|--|\n"
            for t in self._today_trades[-20:]:
                b = -t['before'] if t['before'] != 0 else 0
                a = -t['after'] if t['after'] != 0 else 0
                trade_info += (f"| {t['time']} | {t['action']} | "
                               f"{t['lots']}({t['side']}) | {t['price']} | "
                               f"{b}->{a} |\n")
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

        # 用实际成交价更新avg_price (做空)
        pos = self.get_position(self.params_map.instrument_id)
        actual = abs(pos.net_position) if pos else 0
        if direction == "sell" and actual > 0:
            old_pos = max(0, actual - trade.volume)
            if old_pos > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old_pos + trade.price * trade.volume) / actual
            else:
                self.avg_price = trade.price
            if hasattr(self, 'trough_price'):
                if trade.price < self.trough_price or self.trough_price == 0:
                    self.trough_price = trade.price
        elif direction == "buy" and actual <= 0:
            self.avg_price = 0.0
            if hasattr(self, 'trough_price'):
                self.trough_price = 0.0

        self.state_map.net_pos = pos.net_position if pos else 0  # 做空显示原始负值
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
