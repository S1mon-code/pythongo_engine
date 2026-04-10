"""
================================================================================
  I_Short_1H_V26 — OI Flow + MACD 做空 (铁矿石 H1)
================================================================================

  信号: OI Flow bearish + MACD confirms downward → [-1, 0]
  止损: Chandelier Exit(Short) + RiskManager全套止损
  仓位: Vol Targeting + Carver 10% buffer, 四舍五入到整数手
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


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "I_Short_1H_V26"

# 策略指标参数
OI_PERIOD = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9
FLOW_THRESHOLD = 0.2
WARMUP = 40

# Chandelier Exit (Short)
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 2.5

# Vol Targeting
FORECAST_SCALAR = 10.0
FORECAST_CAP = 20.0
ANNUAL_FACTOR = 252 * 7          # H1: 铁矿石 ~7 bars/day

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


def atr(highs, lows, closes, period=14):
    """ATR with Wilder RMA smoothing."""
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
#  SIGNAL
# ══════════════════════════════════════════════════════════════════════════════

def generate_signal(closes, oi, volumes, bar_idx):
    """OI Flow + MACD 做空信号 → [-1, 0]."""
    if bar_idx < WARMUP:
        return 0.0

    flow, _ = _oi_flow(closes, oi, volumes, OI_PERIOD)
    macd_line, macd_sig, _ = _macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL_PERIOD)

    of = flow[bar_idx]
    ml = macd_line[bar_idx]
    ms = macd_sig[bar_idx]

    if np.isnan(of) or np.isnan(ml) or np.isnan(ms):
        return 0.0

    score = 0.0
    if of < -FLOW_THRESHOLD:
        score += 1.0
    if ml < ms:
        score += 1.0
    if ml < 0:
        score += 0.5

    if score >= 1.5:
        return float(np.clip(-0.8 * min(1.0, score / 2.5), -1.0, 0.0))
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
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2609", title="合约代码")
    kline_style: str = Field(default="H1", title="K线周期")
    max_lots: int = Field(default=5, title="最大持仓")
    capital: float = Field(default=5_000_000, title="配置资金")
    hard_stop_pct: float = Field(default=0.5, title="硬止损(%)")
    trailing_pct: float = Field(default=0.3, title="移动止损(%)")
    equity_stop_pct: float = Field(default=2.0, title="权益止损(%)")
    flatten_minutes: int = Field(default=5, title="盘前清仓(分钟)")
    sim_24h: bool = Field(default=False, title="24H模拟盘模式")


class State(BaseState):
    signal: float = Field(default=0.0, title="信号")
    forecast: float = Field(default=0.0, title="预测")
    target_lots: int = Field(default=0, title="目标手")
    net_pos: int = Field(default=0, title="净持仓")
    avg_price: float = Field(default=0.0, title="均价")
    trough_price: float = Field(default=0.0, title="谷价(空)")
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

class I_Short_1H_V26_OI_Flow_MACD(BaseStrategy):
    """铁矿石H1做空 — OI Flow + MACD"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        # OI data
        self._oi_data = []

        # 持仓状态
        self.avg_price = 0.0
        self.trough_price = 0.0
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
            self.trough_price = saved.get("trough_price", 0.0)
            self.avg_price = saved.get("avg_price", 0.0)
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
        actual = abs(pos.net_position) if pos else 0
        self.state_map.net_pos = -actual
        if actual == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0

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
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)
        p = self.params_map

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
    #  K线回调
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        try:
            self.output(f"[H1 BAR] {kline.datetime} close={kline.close:.1f} trading={self.trading}")
            self._on_bar(kline)
        except Exception as e:
            self.output(f"[callback异常] {type(e).__name__}: {e}")

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    def _on_bar(self, kline: KLineData):
        signal_price = 0.0
        p = self.params_map

        # 收集OI
        self._oi_data.append(kline.open_interest)

        # 撤挂单
        for oid in list(self.order_id):
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
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # 数据准备
        producer = self.kline_generator.producer
        closes = np.array(producer.close, dtype=np.float64)
        highs = np.array(producer.high, dtype=np.float64)
        lows = np.array(producer.low, dtype=np.float64)
        volumes = np.array(producer.volume, dtype=np.float64)
        oi = np.array(self._oi_data, dtype=np.float64)
        bar_idx = len(closes) - 1

        # OI对齐: push_history_data可能多推几根bar
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

        close = float(closes[-1])

        # 信号计算 (指标debug)
        flow, _ = _oi_flow(closes, oi, volumes, OI_PERIOD)
        macd_line, macd_sig, _ = _macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL_PERIOD)
        of_val = flow[bar_idx] if not np.isnan(flow[bar_idx]) else 0
        ml_val = macd_line[bar_idx] if not np.isnan(macd_line[bar_idx]) else 0
        ms_val = macd_sig[bar_idx] if not np.isnan(macd_sig[bar_idx]) else 0
        self.output(f"[IND] OI_Flow={of_val:.4f} MACD={ml_val:.1f} Signal={ms_val:.1f} close={close:.1f}")

        raw = generate_signal(closes, oi, volumes, bar_idx)
        forecast = min(FORECAST_CAP, max(0.0, abs(raw) * FORECAST_SCALAR))
        self.state_map.signal = round(raw, 3)
        self.state_map.forecast = round(forecast, 1)
        self.output(f"[SIGNAL] raw={raw:.4f} forecast={forecast:.1f}")

        # 仓位计算 (四舍五入到整数手)
        atr_arr = atr(highs, lows, closes)
        optimal_raw = calc_optimal_lots(
            forecast, atr_arr[bar_idx], close,
            p.capital, p.max_lots, self._multiplier, ANNUAL_FACTOR,
        )
        optimal = round(optimal_raw)  # 四舍五入到整数
        net_pos = abs(self.get_position(p.instrument_id).net_position)
        target = apply_buffer(optimal, net_pos)
        target = min(target, p.max_lots)  # 绝对不超过max_lots
        self.state_map.net_pos = -net_pos
        self.state_map.target_lots = -target

        # 持仓追踪 (空头: trough=最低价)
        if net_pos == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0
        elif self.trough_price == 0.0 or close < self.trough_price:
            self.trough_price = close
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.trough_price = round(self.trough_price, 1)
        self.state_map.hard_line = (
            round(self.avg_price * (1 + p.hard_stop_pct / 100), 1) if net_pos > 0 else 0.0
        )
        self.state_map.trail_line = (
            round(self.trough_price * (1 + p.trailing_pct / 100), 1) if net_pos > 0 else 0.0
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
        #     self._push_widget(kline, kline.close)
        #     self.update_status_bar()
        #     return

        # ── 非交易时段 ──
        if not self._guard.should_trade():
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # ── 止损检查 (空头: 反向) ──
        if net_pos > 0:
            if self.avg_price > 0 and close >= self.avg_price * (1 + p.hard_stop_pct / 100):
                self._pending = "HARD_STOP"
                self._pending_reason = (
                    f"硬止损(空) close={close:.1f} >= "
                    f"avg*(1+{p.hard_stop_pct}%)={self.avg_price * (1 + p.hard_stop_pct / 100):.1f}"
                )
                self.output(f"[HARD_STOP] {self._pending_reason}")
                self.state_map.pending = "HARD_STOP"
                self._push_widget(kline, signal_price)
                self.update_status_bar()
                return

            if self.trough_price > 0 and close >= self.trough_price * (1 + p.trailing_pct / 100):
                self._pending = "TRAIL_STOP"
                self._pending_reason = (
                    f"移动止损(空) close={close:.1f} >= "
                    f"trough*(1+{p.trailing_pct}%)={self.trough_price * (1 + p.trailing_pct / 100):.1f}"
                )
                self.output(f"[TRAIL_STOP] {self._pending_reason}")
                self.state_map.pending = "TRAIL_STOP"
                self._push_widget(kline, signal_price)
                self.update_status_bar()
                return

            action, reason = self._risk.check(
                close=close, avg_price=self.avg_price, peak_price=self.avg_price,
                pos_profit=pos_profit, net_pos=net_pos,
                hard_stop_pct=999.0, trailing_pct=999.0,
                equity_stop_pct=p.equity_stop_pct,
            )
            if action and action not in ("WARNING", "HARD_STOP", "TRAIL_STOP"):
                self._pending = action
                self._pending_reason = reason
                self.output(f"[{action}] {reason}")
            elif action == "WARNING":
                self.output(f"[预警] {reason}")
                feishu("warning", p.instrument_id, f"**回撤预警**: {reason}")

        # ── Chandelier Exit (Short) ──
        if self._pending is None and net_pos > 0:
            ch_atr = atr(highs, lows, closes, CHANDELIER_PERIOD)
            if chandelier_short(lows, closes, ch_atr, bar_idx):
                self._pending = "CLOSE"
                self._pending_reason = "Chandelier Exit (Short)"
                self.output(f"[CHANDELIER] {self._pending_reason}")

        # ── 正常信号 → pending ──
        if self._pending is None and target != net_pos:
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

        self.state_map.pending = self._pending or "---"
        self.state_map.slippage = self._slip.format_report()
        self.state_map.perf = self._perf.format_short()
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ══════════════════════════════════════════════════════════════════════
    #  执行 (SHORT: open=sell, close=buy)
    # ══════════════════════════════════════════════════════════════════════

    def _aggressive_price(self, price, direction):
        """SHFE实盘不支持市价卖单，用当前价限价单代替."""
        return price

    def _execute(self, kline: KLineData, action: str) -> float:
        price = kline.close
        p = self.params_map
        actual = abs(self.get_position(p.instrument_id).net_position)

        if action == "OPEN":
            target = self._pending_target or 1
            vol = max(1, target)
            acct = self._get_account()
            if acct and price * self._multiplier * vol * 0.15 > acct.available * 0.6:
                self.output("[保证金不足]")
                feishu("error", p.instrument_id, f"**保证金不足** 需开{vol}手")
                return 0.0
            self._slip.set_signal_price(price)
            sell_price = self._aggressive_price(price, "sell")
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=sell_price, order_direction="sell",
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
                   f"逻辑: {self._pending_reason}\n"
                   f"持仓: {actual} -> {actual + vol}手")
            self._save()
            return -price

        elif action == "ADD":
            target = self._pending_target or (actual + 1)
            vol = max(1, target - actual)
            acct = self._get_account()
            if acct and price * self._multiplier * vol * 0.15 > acct.available * 0.6:
                self.output("[加仓保证金不足]")
                return 0.0
            self._slip.set_signal_price(price)
            sell_price = self._aggressive_price(price, "sell")
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=sell_price, order_direction="sell",
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
                   f"逻辑: {self._pending_reason}\n"
                   f"均价: {self.avg_price:.1f}\n"
                   f"持仓: {actual} -> {actual + vol}手")
            self._save()
            return -price

        elif action == "REDUCE":
            vol = max(1, actual // 2)
            if actual <= 0:
                return 0.0
            self._slip.set_signal_price(price)
            buy_price = self._aggressive_price(price, "buy")
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=buy_price, order_direction="buy",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.state_map.last_action = f"减空{vol}手"
            self._rec("减空", vol, "买", price, actual, actual - vol)
            feishu("reduce", p.instrument_id,
                   f"**减空** {vol}手 @ {price:,.1f}\n"
                   f"逻辑: {self._pending_reason}\n"
                   f"持仓: {actual} -> {actual - vol}手")
            self._save()
            return price

        elif action in ("CLOSE", "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP",
                         "CIRCUIT", "DAILY_STOP", "FLATTEN"):
            return self._exec_close(kline, actual, action)

        return 0.0

    def _exec_close(self, kline: KLineData, actual: int, action: str) -> float:
        """统一平仓逻辑 (空头: 买入平仓)."""
        labels = {
            "CLOSE": "信号平仓", "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损",
            "EQUITY_STOP": "权益止损", "CIRCUIT": "熔断", "DAILY_STOP": "单日止损",
            "FLATTEN": "盘前清仓",
        }
        label = labels.get(action, action)
        p = self.params_map
        price = kline.close

        if actual <= 0:
            return 0.0
        self._slip.set_signal_price(price)
        buy_price = self._aggressive_price(price, "buy")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=actual, price=buy_price, order_direction="buy",
        )
        if oid is not None:
            self.order_id.add(oid)
            self._om.on_send(oid, actual, price)
        pnl_pct = (self.avg_price - price) / self.avg_price * 100 if self.avg_price > 0 else 0
        abs_pnl = self._perf.on_close(self.avg_price, price, actual)
        self.state_map.last_action = f"{label} {pnl_pct:+.2f}%"
        self._rec(label, actual, "买", price, actual, 0)
        feishu(action.lower(), p.instrument_id,
               f"**{label}** {actual}手 @ {price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})\n"
               f"持仓: {actual} -> 0手")
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
        state.update(self._risk.get_state())
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
        slip = self._slip.on_fill(
            trade.price, trade.volume,
            "buy" if "买" in str(trade.direction) else "sell",
        )
        if slip != 0:
            self.output(f"[滑点] {slip:.1f}ticks")
        self.state_map.net_pos = self.get_position(
            self.params_map.instrument_id
        ).net_position
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)
        self._om.on_cancel(order.order_id)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
