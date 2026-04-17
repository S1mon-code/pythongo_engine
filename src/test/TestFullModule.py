"""
================================================================================
  TestFullModule — M1 双均线 + 全模块集成测试
================================================================================

  目的: 验证全部12个模块在PythonGO实盘环境中协同工作
  信号: MA(3) vs MA(7) 金叉做多, 死叉平仓, 展幅>0.1%加仓
  部署: modules/ 放在 pyStrategy/modules/, 本文件放 self_strategy/

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


def _freq_to_sec(kline_style) -> int:
    """防御性 str / enum / KLineStyleType 解析."""
    mapping = {
        "M1": 60, "M3": 180, "M5": 300, "M15": 900, "M30": 1800,
        "H1": 3600, "H2": 7200, "H4": 14400,
        "D1": 86400, "W1": 604800,
    }
    for getter in (lambda x: str(x), lambda x: getattr(x, "value", None),
                   lambda x: getattr(x, "name", None)):
        try:
            raw = getter(kline_style)
            if raw is None:
                continue
            key = str(raw).upper()
            if "." in key:
                key = key.rsplit(".", 1)[-1]
            if key in mapping:
                return mapping[key]
        except Exception:
            continue
    return 3600


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

VOL_ATR_PERIOD = 14
ANNUAL_FACTOR = 252 * 240       # M1 bars/year
DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15
ADD_SPREAD_THRESHOLD = 0.001    # 展幅>0.1%才加仓


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATOR
# ══════════════════════════════════════════════════════════════════════════════

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
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2609", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")
    fast_period: int = Field(default=3, title="快线周期")
    slow_period: int = Field(default=7, title="慢线周期")
    unit_volume: int = Field(default=1, title="每次手数")
    max_lots: int = Field(default=3, title="最大持仓")
    capital: float = Field(default=1_000_000, title="配置资金")
    hard_stop_pct: float = Field(default=0.5, title="硬止损(%)")
    trailing_pct: float = Field(default=0.3, title="移动止损(%)")
    equity_stop_pct: float = Field(default=2.0, title="权益止损(%)")
    flatten_minutes: int = Field(default=5, title="即将收盘提示(分钟)")
    sim_24h: bool = Field(default=True, title="24H模拟盘模式")


class State(BaseState):
    fast_ma: float = Field(default=0.0, title="快均线")
    slow_ma: float = Field(default=0.0, title="慢均线")
    net_pos: int = Field(default=0, title="净持仓")
    avg_price: float = Field(default=0.0, title="均价")
    peak_price: float = Field(default=0.0, title="最高价")
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

class TestFullModule(BaseStrategy):
    """M1 双均线 + 全模块集成测试"""

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
        self._pending_reason = ""      # 触发逻辑描述
        self.order_id = set()

        # 权益追踪
        self._investor_id = ""
        self._risk = None  # RiskManager，在on_start初始化
        self._current_td = ""
        self._daily_review_sent = False
        self._rollover_checked = False
        self._today_trades = []

        # 模块实例 (需要instrument_id的在on_start中初始化)
        self._guard = None
        self._slip = None
        self._hb = None
        self._om = OrderMonitor()
        self._perf = None
        self._pricer: AggressivePricer | None = None   # on_start 时按 tick_size 初始化
        self._multiplier = 100

        # Scaled entry (2026-04-17)
        self._rvwap: RollingVWAP | None = None
        self._entry: ScaledEntryExecutor | None = None
        self._rvwap_prev_vol = 0

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

        # 合约参数 (从contract_info获取, 不再硬编码)
        self._multiplier = get_multiplier(p.instrument_id)
        self._pricer = AggressivePricer(tick_size=get_tick_size(p.instrument_id))

        # Scaled entry (2026-04-17)
        self._rvwap = RollingVWAP(window_seconds=1800)
        self._entry = ScaledEntryExecutor(EntryParams(bottom_lots=2))
        self._rvwap_prev_vol = 0

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

        # 风控管理器（21:00 day start）
        self._risk = RiskManager(capital=p.capital)

        # 恢复状态
        saved = load_state("TestFullModule")
        if saved:
            self._risk.load_state(saved)
            self.peak_price = saved.get("peak_price", 0.0)
            self.avg_price = saved.get("avg_price", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._today_trades = saved.get("today_trades", [])
            self.output(f"[恢复] peak_eq={self._risk.peak_equity:.0f} avg={self.avg_price:.1f}")

        # 权益初始化
        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance)

        # 信任broker持仓
        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        # 换月检查
        level, days = check_rollover(p.instrument_id)
        if level:
            feishu("rollover", p.instrument_id, f"**换月提醒**: 距交割月**{days}天**")

        super().on_start()
        self.output(
            f"启动 | {p.instrument_id} {p.kline_style} | "
            f"乘数={self._multiplier} | 持仓={actual}"
        )
        feishu("start", p.instrument_id,
               f"**策略启动**\n合约: {p.instrument_id}\n乘数: {self._multiplier}\n持仓: {actual}手")

    def on_stop(self):
        self._save()
        feishu("shutdown", self.params_map.instrument_id,
               f"**策略停止**\n持仓: {self.state_map.net_pos}手\n"
               f"{self._slip.format_report()}")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)
        p = self.params_map

        # 喂 pricer + RollingVWAP
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

        # Escalator (legacy, 保留)
        if (self._guard is not None and self._guard.should_trade()
                and self._pricer is not None):
            for oid, next_urgency, info in self._om.check_escalation():
                self._resubmit_escalated(oid, next_urgency, info)

        # Scaled entry 驱动 (2026-04-17)
        try:
            self._drive_entry(tick)
        except Exception as e:
            self.output(f"[entry异常] {type(e).__name__}: {e}")

        try:
            # 交易日切换（21:00 day start）
            td = get_trading_day()
            if td != self._current_td and self._current_td:
                acct = self._get_account()
                if acct:
                    self._risk.on_day_change(acct.balance)  # 重置daily_start_eq
                self._perf.on_day_change()  # 重置每日PnL
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
        except Exception as e:
            self.output(f"[on_tick异常] {type(e).__name__}: {e}")

    # ══════════════════════════════════════════════════════════════════════
    #  K线回调
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        try:
            self._on_bar(kline)
        except Exception as e:
            self.output(f"[callback异常] {type(e).__name__}: {e}")

    def _on_bar(self, kline: KLineData):
        signal_price = 0.0
        p = self.params_map

        # 撤挂单
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

        # 执行pending (next-bar规则)
        if self._pending is not None:
            action = self._pending
            if action in ("OPEN", "ADD") and self._entry is not None:
                # 2026-04-17: 入场走 ScaledEntryExecutor
                net_pos = self.get_position(p.instrument_id).net_position
                bar_total = _freq_to_sec(p.kline_style)
                actions = self._entry.on_signal(
                    target=self._pending_target or 1, direction="buy",
                    now=datetime.now(), current_position=net_pos,
                    forecast=5.0,  # TestFullModule 无 forecast, 用默认
                    bar_total_sec=bar_total,
                )
                for ea in actions:
                    self._apply_entry_action(ea)
                signal_price = 0.0
            else:
                signal_price = self._execute(kline, action)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # 数据检查
        producer = self.kline_generator.producer
        if len(producer.close) < p.slow_period + 2:
            self._push_widget(kline, signal_price)
            return

        # 指标 (不用producer.sma, 手动算避免ta_sma bad parameter)
        closes = np.array(producer.close, dtype=np.float64)
        close = float(closes[-1])
        fast_ma = float(np.mean(closes[-p.fast_period:]))
        slow_ma = float(np.mean(closes[-p.slow_period:]))
        self.state_map.fast_ma = round(fast_ma, 2)
        self.state_map.slow_ma = round(slow_ma, 2)

        # 持仓
        net_pos = self.get_position(p.instrument_id).net_position
        self.state_map.net_pos = net_pos
        if net_pos == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0
        elif close > self.peak_price:
            self.peak_price = close
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.peak_price = round(self.peak_price, 1)
        self.state_map.hard_line = (
            round(self.avg_price * (1 - p.hard_stop_pct / 100), 1) if net_pos > 0 else 0.0
        )
        self.state_map.trail_line = (
            round(self.peak_price * (1 - p.trailing_pct / 100), 1) if net_pos > 0 else 0.0
        )

        # 权益（RiskManager自动追踪peak和daily）
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
        # # ── 盘前清仓 (优先级最高, 立即执行不等next-bar) ──
        # if self._guard.should_flatten() and net_pos > 0:
        #     self._pending_reason = f"距收盘<{p.flatten_minutes}分钟, 自动清仓"
        #     self._exec_close(kline, net_pos, "FLATTEN")
        #     self._push_widget(kline, -kline.close)
        #     self.update_status_bar()
        #     return

        # ── 非交易时段不生成新信号 ──
        if not self._guard.should_trade():
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # ── 止损检查（RiskManager管理daily_start_eq，21:00重置）──
        action, reason = self._risk.check(
            close=close, avg_price=self.avg_price, peak_price=self.peak_price,
            pos_profit=pos_profit, net_pos=net_pos,
            hard_stop_pct=p.hard_stop_pct,
            trailing_pct=p.trailing_pct, equity_stop_pct=p.equity_stop_pct,
        )
        if action and action != "WARNING":
            self._pending = action
            self._pending_reason = reason
            self.output(f"[{action}] {reason}")
        elif action == "WARNING":
            self.output(f"[预警] {reason}")
            feishu("warning", p.instrument_id, f"**回撤预警**: {reason}")

        # ── 正常信号 ──
        if self._pending is None:
            bullish = fast_ma > slow_ma
            spread = abs(fast_ma - slow_ma) / slow_ma if slow_ma > 0 else 0

            if net_pos == 0 and bullish:
                self._pending = "OPEN"
                self._pending_target = p.unit_volume
                self._pending_reason = (
                    f"金叉: MA{p.fast_period}={fast_ma:.1f} > MA{p.slow_period}={slow_ma:.1f}, "
                    f"展幅={spread:.4%}"
                )
            elif net_pos > 0 and bullish and net_pos < p.max_lots and spread > ADD_SPREAD_THRESHOLD:
                self._pending = "ADD"
                self._pending_target = min(net_pos + p.unit_volume, p.max_lots)
                self._pending_reason = (
                    f"趋势加强: 展幅={spread:.4%} > {ADD_SPREAD_THRESHOLD:.4%}, "
                    f"MA{p.fast_period}={fast_ma:.1f} > MA{p.slow_period}={slow_ma:.1f}"
                )
            elif net_pos > 0 and not bullish:
                self._pending = "CLOSE"
                self._pending_target = 0
                self._pending_reason = (
                    f"死叉: MA{p.fast_period}={fast_ma:.1f} <= MA{p.slow_period}={slow_ma:.1f}"
                )

        # ── 当前bar立即处理pending (不等下一根bar) ──
        if self._pending is not None:
            action = self._pending
            if action in ("OPEN", "ADD") and self._entry is not None:
                # 2026-04-17: 入场走 ScaledEntryExecutor
                bar_total = _freq_to_sec(p.kline_style)
                actions = self._entry.on_signal(
                    target=self._pending_target or 1, direction="buy",
                    now=datetime.now(), current_position=net_pos,
                    forecast=5.0,
                    bar_total_sec=bar_total,
                )
                for ea in actions:
                    self._apply_entry_action(ea)
                signal_price = 0.0
            else:
                signal_price = self._execute(kline, action)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""

        self.state_map.pending = self._pending or "---"
        self.state_map.slippage = self._slip.format_report()
        self.state_map.perf = self._perf.format_short()
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    # ══════════════════════════════════════════════════════════════════════
    #  执行
    # ══════════════════════════════════════════════════════════════════════

    def _drive_entry(self, tick: TickData) -> None:
        """每 tick 驱动 scaled entry executor (2026-04-17)."""
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
            forecast=5.0,
            current_position=net_pos,
        )
        for a in actions:
            self._apply_entry_action(a)

    def _apply_entry_action(self, a: ExecAction) -> None:
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

    def _aggressive_price(self, price, direction, urgency: str = "normal"):
        """Spread-aware 限价定价 (替代 market=True).

        urgency: passive(入场) / normal(减仓/信号CLOSE) / cross(VWAP) /
                 urgent(硬/移止损) / critical(熔断/权益/单日/FLATTEN)
        """
        if self._pricer is None or self._pricer.last == 0:
            return price
        return self._pricer.price(direction, urgency)

    def _resubmit_escalated(self, old_oid, next_urgency: str, info: dict) -> None:
        """Escalator: 撤掉未成交订单, 按 next_urgency 重挂."""
        direction = info.get("direction")
        kind = info.get("kind")
        vol = info.get("vol", 0)
        if not direction or not kind or vol <= 0:
            return
        if self._pricer is None or self._pricer.last == 0:
            return
        p = self.params_map

        self.cancel_order(old_oid)
        self._om.on_cancel(old_oid)
        self.order_id.discard(old_oid)

        new_price = self._pricer.price(direction, next_urgency)
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

    def _execute(self, kline: KLineData, action: str) -> float:
        price = kline.close
        p = self.params_map
        actual = self.get_position(p.instrument_id).net_position

        if action == "OPEN":
            target = self._pending_target or p.unit_volume
            vol = max(1, target)
            # 保证金检查
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
            target = self._pending_target or (actual + p.unit_volume)
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
            sell_price = self._aggressive_price(price, "sell", urgency="normal")
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=sell_price, order_direction="sell",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, sell_price,
                                 urgency="normal", direction="sell", kind="reduce")
            self.state_map.last_action = f"回撤减仓{vol}手"
            self._rec("回撤减仓", vol, "卖", price, actual, actual - vol)
            feishu("reduce", p.instrument_id,
                   f"**回撤减仓** {vol}手 @ {price:,.1f}\n"
                   f"逻辑: {self._pending_reason}\n"
                   f"持仓: {actual} -> {actual - vol}手")
            self._save()
            return -price

        elif action in ("CLOSE", "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP",
                         "CIRCUIT", "DAILY_STOP", "FLATTEN"):
            return self._exec_close(kline, actual, action)

        return 0.0

    def _exec_close(self, kline: KLineData, actual: int, action: str) -> float:
        """统一平仓逻辑."""
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
        # 止损类 urgent, 熔断/权益/单日/FLATTEN critical, 信号 CLOSE normal
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
            "trading_day": self._current_td,
            "today_trades": self._today_trades[-50:],
        }
        state.update(self._risk.get_state())  # peak_equity, daily_start_eq
        save_state(state, name="TestFullModule")

    def _send_review(self):
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        net = pos.net_position if pos else 0
        acct = self._get_account()
        eq = acct.balance if acct else 0
        dd_pct = self._risk.drawdown_pct * 100
        daily_pct = self._risk.daily_pnl_pct * 100
        daily_abs = eq - self._risk.daily_start_eq
        if self._today_trades:
            tbl = "| 时间 | 操作 | 手数 | 价格 | 持仓 |\n|--|--|--|--|--|\n"
            for t in self._today_trades[-10:]:
                tbl += (f"| {t['time']} | {t['action']} | "
                        f"{t['lots']}({t['side']}) | {t['price']} | "
                        f"{t['before']}->{t['after']} |\n")
        else:
            tbl = "无交易"
        feishu("daily_review", p.instrument_id,
               f"**每日回顾** (21:00起算)\n"
               f"权益: {eq:,.0f} | 回撤: {dd_pct:.2f}%\n"
               f"当日PnL: {daily_abs:+,.0f} ({daily_pct:+.2f}%)\n"
               f"当日交易: {self._perf.daily_trade_count}笔 PnL{self._perf.daily_pnl:+,.0f}\n"
               f"持仓: {net}手\n\n{tbl}\n\n"
               f"{self._slip.format_report()}\n{self._perf.format_report(p.instrument_id)}")

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
        # Scaled entry (2026-04-17, audit v2: 返回值用于隔离)
        if self._entry is not None:
            self._entry.on_trade(trade.order_id, trade.price, trade.volume, datetime.now())
        slip = self._slip.on_fill(
            trade.price, trade.volume,
            "buy" if "买" in str(trade.direction) else "sell",
        )
        if slip != 0:
            self.output(f"[滑点] {slip:.1f}ticks")
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        direction = "buy" if "买" in str(trade.direction) else "sell"
        if direction == "buy" and actual > 0:
            old_pos = max(0, actual - trade.volume)
            if old_pos > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old_pos + trade.price * trade.volume) / actual
            else:
                self.avg_price = trade.price
        elif direction == "sell" and actual == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0
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
