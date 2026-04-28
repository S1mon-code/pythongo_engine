"""
================================================================================
  AG_Long_5M_MomentumContinuation — QExp robust 策略移植
================================================================================

  原始: QExp overlay/signals/momentum_continuation.py
        + config/symbols/ag_momentum_continuation.yaml
  Audit: 2026-04-26 momentum-family, 8y Sharpe +0.908, ex-best Δ -0.09 (严格 ROBUST)

  信号 (binary fires):
    - 上涨 bar (close > open)
    - body > 1.5 × ATR(20)
    - body / range >= 0.6  (close 在 range top 80%)
    - cooldown >= 3 bars

  入场: signal fires → 直接开 3 手 (max_lots=capacity, 不走 Carver)
  出场 (任一即平):
    - profit_target: close >= entry + 2 × entry_ATR
    - hard_stop:    close <= entry × (1 − 2%)

  Session skip windows (开盘后 15min 不发新信号):
    - 09:00-09:15 (日盘)
    - 13:30-13:45 (午盘)
    - 21:00-21:15 (夜盘)
    - 02:00-02:30 (AG 夜盘后段, 收盘前)
================================================================================
"""
import sys
import time as _time
from datetime import datetime, time

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.contract_info import get_multiplier, get_tick_size
from modules.error_handler import throttle_on_error
from modules.feishu import feishu
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.persistence import load_state, save_state
from modules.pricing import AggressivePricer
from modules.qexp_signals import MomentumContinuationSignal
from modules.rollover import check_rollover
from modules.session_guard import SessionGuard
from modules.slippage import SlippageTracker
from modules.trading_day import get_trading_day


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "AG_Long_5M_MomentumContinuation"

# QExp config: capacity=3, hard_stop=2%, profit_target=2×ATR
MAX_LOTS = 3
HARD_STOP_PCT = 2.0
PROFIT_TARGET_ATR_MULT = 2.0

# Session skip windows (开盘 15min 不发新信号)
SKIP_WINDOWS = [
    (time(9, 0), time(9, 15)),
    (time(13, 30), time(13, 45)),
    (time(21, 0), time(21, 15)),
    (time(2, 0), time(2, 30)),    # AG 夜盘后段
]

# 日报时间
DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15


def _freq_to_sec(kline_style) -> int:
    mapping = {"M1": 60, "M3": 180, "M5": 300, "M15": 900, "M30": 1800,
               "H1": 3600, "H4": 14400, "D1": 86400}
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
    return 300  # M5 default


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════


class Params(BaseParams):
    exchange: str = Field(default="SHFE", title="交易所代码")
    instrument_id: str = Field(default="ag2606", title="合约代码")
    kline_style: str = Field(default="M5", title="K线周期")
    max_lots: int = Field(default=MAX_LOTS, title="最大持仓(硬上限3)")
    capital: float = Field(default=1_000_000, title="配置资金")
    hard_stop_pct: float = Field(default=HARD_STOP_PCT, title="硬止损百分比")
    profit_target_atr_mult: float = Field(default=PROFIT_TARGET_ATR_MULT, title="止盈ATR倍数")
    flatten_minutes: int = Field(default=5, title="即将收盘提示分钟")
    sim_24h: bool = Field(default=False, title="24H模拟盘模式")
    # takeover_lots: 启动时手动指定接管手数 (0=按 state 恢复; >0=手动接管, 覆盖 state)
    takeover_lots: int = Field(default=0, title="启动接管手数")


class State(BaseState):
    # 信号相关
    signal_fires: bool = Field(default=False, title="信号触发")
    last_atr: float = Field(default=0.0, title="ATR")
    body_to_range: float = Field(default=0.0, title="body占比")
    body_atr_ratio: float = Field(default=0.0, title="body/ATR")
    # 持仓
    own_pos: int = Field(default=0, title="自管持仓")
    broker_pos: int = Field(default=0, title="账户总持仓")
    my_oids_n: int = Field(default=0, title="已发单累计")
    # 持仓追踪
    avg_price: float = Field(default=0.0, title="均价")
    entry_atr: float = Field(default=0.0, title="入场ATR")
    profit_target: float = Field(default=0.0, title="止盈线")
    hard_stop_line: float = Field(default=0.0, title="硬止损线")
    # UI 实时
    last_price: float = Field(default=0.0, title="最新价")
    last_bid1: float = Field(default=0.0, title="买一价")
    last_ask1: float = Field(default=0.0, title="卖一价")
    spread_tick: int = Field(default=0, title="盘口价差")
    last_tick_time: str = Field(default="---", title="最后tick时间")
    heartbeat: int = Field(default=0, title="心跳")
    tick_count: int = Field(default=0, title="tick计数")
    bar_count: int = Field(default=0, title="bar计数")
    # 账户
    equity: float = Field(default=0.0, title="权益")
    daily_pnl: str = Field(default="---", title="当日盈亏")
    # 状态
    trading_day: str = Field(default="", title="交易日")
    session: str = Field(default="---", title="交易时段")
    pending: str = Field(default="---", title="待执行")
    last_action: str = Field(default="---", title="上次操作")
    skip_window: bool = Field(default=False, title="开盘静默期")
    # 辅助
    slippage: str = Field(default="---", title="滑点")
    perf: str = Field(default="---", title="绩效")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════


class AG_Long_5M_MomentumContinuation(BaseStrategy):
    """AG 5min Momentum Continuation — QExp robust strategy."""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        # 自管持仓
        self._own_pos: int = 0
        self._my_oids: set = set()
        self.avg_price = 0.0
        self._entry_atr = 0.0
        self._takeover_pending = False

        # pending / 挂单
        self._pending: str | None = None
        self._pending_reason = ""
        self.order_id = set()

        # 账户
        self._investor_id = ""
        self._current_td = ""
        self._daily_review_sent = False
        self._rollover_checked = False

        # K 线数据缓存 (供 generate_signal)
        self._opens: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        self._bar_idx_global: int = -1

        # 信号实例 (保留 cooldown 状态)
        self._signal = MomentumContinuationSignal()

        # 模块
        self._guard: SessionGuard | None = None
        self._slip: SlippageTracker | None = None
        self._hb: HeartbeatMonitor | None = None
        self._om = OrderMonitor()
        self._perf: PerformanceTracker | None = None
        self._pricer: AggressivePricer | None = None
        self._multiplier = 15  # AG default

        # 诊断计数器
        self._tick_count = 0
        self._bar_count = 0
        self._widget_err_count = 0
        self._widget_ok_count = 0

    # ══════════════════════════════════════════════════════════════════════
    #  日志封装
    # ══════════════════════════════════════════════════════════════════════

    def _log(self, msg: str) -> None:
        self.output(msg)
        try:
            print(f"[{STRATEGY_NAME}] {msg}", flush=True)
            sys.stdout.flush()
        except Exception:
            pass

    @property
    def main_indicator_data(self):
        """主图: 入场均价 / 止盈线 / 硬止损线."""
        return {
            "Entry": self.state_map.avg_price,
            "ProfitTarget": self.state_map.profit_target,
            "HardStop": self.state_map.hard_stop_line,
        }

    @property
    def sub_indicator_data(self):
        """副图: ATR + body/range."""
        return {
            "ATR": self.state_map.last_atr,
            "BodyToRange": self.state_map.body_to_range,
        }

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    def _in_skip_window(self, now: datetime) -> bool:
        t = now.time()
        for start, end in SKIP_WINDOWS:
            # 跨午夜的 02:00-02:30 直接落在同一日, 不需要特殊处理
            if start <= t < end:
                return True
        return False

    # ══════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════

    def on_start(self):
        p = self.params_map
        self._log(f"[ON_START] 开始初始化 {p.instrument_id} {p.kline_style}")

        if p.max_lots > MAX_LOTS:
            self._log(f"[max_lots] 参数 {p.max_lots} > {MAX_LOTS}, 强制拉回")
            p.max_lots = MAX_LOTS

        self._multiplier = get_multiplier(p.instrument_id)
        self._pricer = AggressivePricer(tick_size=get_tick_size(p.instrument_id))
        self._guard = SessionGuard(p.instrument_id, p.flatten_minutes,
                                   sim_24h=p.sim_24h, open_grace_sec=30)
        self._slip = SlippageTracker(p.instrument_id)
        self._hb = HeartbeatMonitor(p.instrument_id)
        self._perf = PerformanceTracker(p.instrument_id)
        self._log(
            f"[ON_START] 模块初始化 multiplier={self._multiplier} "
            f"tick_size={get_tick_size(p.instrument_id)} sim_24h={p.sim_24h}"
        )

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self._log("[ON_START] KLineGenerator 已创建, push_history_data...")
        self.kline_generator.push_history_data()
        producer = self.kline_generator.producer
        n_history = len(producer.close)
        # 把历史 K 线灌入信号 buffer
        self._opens = list(map(float, producer.open))
        self._highs = list(map(float, producer.high))
        self._lows = list(map(float, producer.low))
        self._closes = list(map(float, producer.close))
        self._bar_idx_global = n_history - 1
        warmup = self._signal.warmup
        self._log(
            f"[ON_START] push_history 完成 producer_bars={n_history} (预热需 {warmup} 根)"
        )

        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id
        self._log(f"[ON_START] investor_id={self._investor_id}")

        saved = load_state(STRATEGY_NAME)
        if saved:
            self._own_pos = int(saved.get("own_pos", 0))
            self._my_oids = set(saved.get("my_oids", []))
            self.avg_price = saved.get("avg_price", 0.0)
            self._entry_atr = saved.get("entry_atr", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._log(
                f"[ON_START 恢复] own_pos={self._own_pos} avg={self.avg_price:.2f} "
                f"entry_atr={self._entry_atr:.2f} my_oids={len(self._my_oids)}"
            )

        acct = self._get_account()
        if acct:
            self._log(
                f"[ON_START 账户] balance={acct.balance:.0f} "
                f"available={acct.available:.0f} position_profit={acct.position_profit:.0f}"
            )

        pos = self.get_position(p.instrument_id)
        broker_pos = pos.net_position if pos else 0
        self.state_map.own_pos = self._own_pos
        self.state_map.broker_pos = broker_pos
        self._log(f"[ON_START 持仓] own_pos={self._own_pos} broker_pos={broker_pos}")

        # Takeover override
        if p.takeover_lots > 0:
            self._own_pos = int(p.takeover_lots)
            self._my_oids = set()
            self._takeover_pending = True
            self.avg_price = 0.0
            self._entry_atr = 0.0
            self.state_map.own_pos = self._own_pos
            self._log(
                f"[ON_START TAKEOVER] 手动接管 {self._own_pos} 手 "
                f"(覆盖 state, broker_pos={broker_pos}, 底仓={broker_pos - self._own_pos} 手不动). "
                f"avg/entry_atr 将在首 tick 用 last_price 兜底."
            )
            feishu("start", p.instrument_id,
                   f"**TAKEOVER 启动** {STRATEGY_NAME}\n"
                   f"接管 {self._own_pos} 手 / broker {broker_pos} 手\n"
                   f"底仓 {broker_pos - self._own_pos} 手不归策略管")

        if self._own_pos == 0:
            self.avg_price = 0.0
            self._entry_atr = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        level, days = check_rollover(p.instrument_id)
        if level:
            feishu("rollover", p.instrument_id, f"**换月提醒**: 距交割月**{days}天**")

        self._log("[ON_START] 调用 super().on_start()")
        super().on_start()

        self._log(
            f"=== 启动完成 === | {p.instrument_id} {p.kline_style} | "
            f"max_lots={p.max_lots} | own_pos={self._own_pos} broker_pos={broker_pos} | "
            f"session={self._guard.get_status()} should_trade={self._guard.should_trade()}"
        )
        feishu("start", p.instrument_id,
               f"**策略启动** {STRATEGY_NAME} (QExp robust)\n"
               f"合约 {p.instrument_id} {p.kline_style}\n"
               f"自管持仓: {self._own_pos}手 (账户总: {broker_pos}手)\n"
               f"max_lots: {p.max_lots} | hard_stop: {p.hard_stop_pct}% | "
               f"profit_target: {p.profit_target_atr_mult}×ATR")

    def on_stop(self):
        self._save()
        self._log(
            f"[ON_STOP] tick_count={self._tick_count} bar_count={self._bar_count} "
            f"own_pos={self._own_pos}"
        )
        feishu("shutdown", self.params_map.instrument_id,
               f"**策略停止** {STRATEGY_NAME}\n自管持仓: {self._own_pos}手")
        super().on_stop()

    def _save(self) -> None:
        state = {
            "own_pos": self._own_pos,
            "my_oids": list(self._my_oids),
            "avg_price": self.avg_price,
            "entry_atr": self._entry_atr,
            "trading_day": self._current_td,
        }
        try:
            save_state(STRATEGY_NAME, state)
        except Exception as e:
            self._log(f"[SAVE 异常] {type(e).__name__}: {e}")

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)

        # Takeover 兜底
        if self._takeover_pending and tick.last_price > 0:
            self.avg_price = float(tick.last_price)
            self._entry_atr = 0.0  # 首 tick 时无 ATR; 暂用 0, hard_stop 仍 work (基于 avg_price × 0.98)
            self._takeover_pending = False
            self._log(
                f"[TAKEOVER FIRST TICK] avg_price={tick.last_price:.2f} "
                f"own_pos={self._own_pos} (entry_atr 未知, profit_target 暂禁用直到下个 bar)"
            )

        self._tick_count += 1
        self.state_map.tick_count = self._tick_count
        self.state_map.heartbeat = self._tick_count % 1000

        # UI 实时字段
        self.state_map.last_price = float(tick.last_price)
        self.state_map.last_bid1 = float(tick.bid_price1)
        self.state_map.last_ask1 = float(tick.ask_price1)
        try:
            ts = get_tick_size(self.params_map.instrument_id) or 0.01
            if tick.ask_price1 > 0 and tick.bid_price1 > 0:
                self.state_map.spread_tick = int(round(
                    (tick.ask_price1 - tick.bid_price1) / ts
                ))
        except Exception:
            pass
        try:
            self.state_map.last_tick_time = tick.datetime.strftime("%H:%M:%S")
        except Exception:
            self.state_map.last_tick_time = str(tick.datetime)

        self.state_map.session = self._guard.get_status() if self._guard else "---"
        self.state_map.skip_window = self._in_skip_window(datetime.now())

        # 主图/副图: 更新止盈/止损线
        if self._own_pos > 0 and self.avg_price > 0:
            p = self.params_map
            self.state_map.hard_stop_line = self.avg_price * (1.0 - p.hard_stop_pct / 100.0)
            if self._entry_atr > 0:
                self.state_map.profit_target = self.avg_price + p.profit_target_atr_mult * self._entry_atr
        else:
            self.state_map.hard_stop_line = 0.0
            self.state_map.profit_target = 0.0

        # 每 10 tick 刷新状态栏
        if self._tick_count % 10 == 0:
            try:
                self.update_status_bar()
                self._widget_ok_count += 1
            except Exception as e:
                self._widget_err_count += 1
                if self._widget_err_count == 1:
                    self._log(f"[WIDGET 异常] {type(e).__name__}: {e}")

        # ── 实盘止损 (tick 级) ──
        try:
            self._on_tick_stops(tick)
        except Exception as e:
            self._log(f"[stops 异常] {type(e).__name__}: {e}")
            feishu("error", self.params_map.instrument_id,
                   f"**on_tick 异常**\n{type(e).__name__}: {e}")

    def _on_tick_stops(self, tick: TickData):
        """Tick 级硬止损 + 止盈 — 只基于 own_pos."""
        if not self.trading:
            return
        if self._guard is not None and not self._guard.should_trade():
            return
        if self._pending is not None:
            return
        if self._own_pos <= 0:
            return
        if self.avg_price <= 0:
            return

        p = self.params_map
        price = float(tick.last_price)
        if price <= 0:
            return

        # 1. Hard stop: price ≤ entry × (1 − hard_stop_pct%)
        hard_line = self.avg_price * (1.0 - p.hard_stop_pct / 100.0)
        if price <= hard_line:
            self._log(f"[HARD_STOP][TICK] price={price:.2f} <= 硬止损线{hard_line:.2f}")
            self._exec_stop_at_tick(price, "HARD_STOP",
                                    f"price={price:.2f} <= entry×(1-{p.hard_stop_pct}%)={hard_line:.2f}")
            return

        # 2. Profit target: price ≥ entry + atr_mult × entry_ATR
        if self._entry_atr > 0:
            tgt = self.avg_price + p.profit_target_atr_mult * self._entry_atr
            if price >= tgt:
                self._log(f"[PROFIT_TARGET][TICK] price={price:.2f} >= 止盈线{tgt:.2f}")
                self._exec_stop_at_tick(price, "PROFIT_TARGET",
                                        f"price={price:.2f} >= entry+{p.profit_target_atr_mult}×ATR={tgt:.2f}")

    def _exec_stop_at_tick(self, price: float, action: str, reason: str) -> None:
        """Tick 触发的止损 / 止盈立即执行 (穿盘口 urgent 卖出)."""
        p = self.params_map
        if self._guard is not None and not self._guard.should_trade():
            return
        vol = self._own_pos
        if vol <= 0:
            return

        # 撤所有挂单
        for oid in list(self.order_id):
            self.cancel_order(oid)

        self._pending = action
        self._pending_reason = reason
        self._slip.set_signal_price(price)
        sell_price = self._pricer.aggressive(price, "sell", urgency="urgent")
        self._log(f"[EXEC_STOP] {action} auto_close sell {vol}手 @ {sell_price} (urgent)")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=sell_price, order_direction="sell",
        )
        if oid:
            self._my_oids.add(oid)
            self.order_id.add(oid)
            self._om.register(oid)

    # ══════════════════════════════════════════════════════════════════════
    #  Bar callback
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData) -> None:
        self._on_bar(kline, is_realtime=False)

    def real_time_callback(self, kline: KLineData) -> None:
        self._on_bar(kline, is_realtime=True)

    def _on_bar(self, kline: KLineData, is_realtime: bool) -> None:
        self._bar_count += 1
        self.state_map.bar_count = self._bar_count

        # 更新 K 线 buffer (即使非交易日也更新, 信号需要历史)
        # 注意: KLineGenerator 已确保 kline 是 finished bar
        self._opens.append(float(kline.open))
        self._highs.append(float(kline.high))
        self._lows.append(float(kline.low))
        self._closes.append(float(kline.close))
        self._bar_idx_global += 1

        # cancel 所有挂单 (每 bar 开头清理)
        for oid in list(self.order_id):
            self.cancel_order(oid)

        # session 门控
        if self._guard is not None and not self._guard.should_trade():
            self.state_map.session = self._guard.get_status()
            return

        # 信号计算
        opens = np.asarray(self._opens, dtype=float)
        highs = np.asarray(self._highs, dtype=float)
        lows = np.asarray(self._lows, dtype=float)
        closes = np.asarray(self._closes, dtype=float)

        result = self._signal.compute(opens, highs, lows, closes, self._bar_idx_global)
        self.state_map.signal_fires = result.fires
        self.state_map.last_atr = result.atr
        self.state_map.body_to_range = result.metadata.get("body_to_range", 0.0)
        if result.atr > 0:
            self.state_map.body_atr_ratio = result.metadata.get("body", 0.0) / result.atr

        self._log(
            f"[ON_BAR{'实盘' if is_realtime else '历史'}] bar#{self._bar_count} "
            f"close={kline.close} own_pos={self._own_pos} signal={result.metadata.get('state')}"
        )

        # 已有仓位 → 等止损/止盈触发, 不重复开仓
        if self._own_pos > 0:
            return

        # 信号未触发 → 不开仓
        if not result.fires:
            return

        # Skip window 检查 (开盘 15min 不发新信号)
        now = datetime.now()
        if self._in_skip_window(now):
            self._log(f"[SKIP_WINDOW] {now.time()} 在 skip window, 信号忽略")
            return

        # 资金检查
        acct = self._get_account()
        if acct:
            need = result.entry_price * self._multiplier * self.params_map.max_lots * 0.15
            limit = acct.available * 0.6
            if need > limit:
                self._log(
                    f"[保证金不足] need={need:,.0f} limit(60%)={limit:,.0f}, 跳过 OPEN"
                )
                feishu("error", self.params_map.instrument_id, "**保证金不足** 跳过 OPEN")
                return

        # 入场!
        self._exec_open(result.entry_price, result.atr)

    def _exec_open(self, signal_price: float, atr: float) -> None:
        """开 max_lots 手 long."""
        p = self.params_map
        vol = p.max_lots
        if vol <= 0:
            return

        self._pending = "OPEN"
        self._slip.set_signal_price(signal_price)
        buy_price = self._pricer.aggressive(signal_price, "buy", urgency="passive")
        self._log(
            f"[EXEC_OPEN] send_order buy {vol}手 @ {buy_price} (passive) "
            f"signal_price={signal_price:.2f} atr={atr:.2f}"
        )
        oid = self.send_order(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=buy_price, order_direction="buy", offset="open",
        )
        if oid:
            self._my_oids.add(oid)
            self.order_id.add(oid)
            self._om.register(oid)
            # entry_atr 在成交回调里 set (用真实 fill price 算 hard_stop / profit_target)
            self._entry_atr = atr  # ATR 提前 cache, fill 后用 fill_price 重算 avg
            self._log(f"[EXEC_OPEN] oid={oid} 已提交")

    # ══════════════════════════════════════════════════════════════════════
    #  Trade / Order callbacks
    # ══════════════════════════════════════════════════════════════════════

    def on_trade(self, trade: TradeData) -> None:
        super().on_trade(trade)
        oid = trade.order_id

        # 自管过滤: 不在 my_oids 里的成交跳过
        if oid not in self._my_oids:
            self._log(f"[ON_TRADE] oid={oid} 非本策略, 跳过")
            return

        direction = "buy" if str(trade.direction).lower() in ("buy", "0", "买") else "sell"
        offset_str = str(trade.offset)
        is_open = "open" in offset_str.lower() or offset_str == "0"
        vol = int(trade.volume)
        price = float(trade.price)

        self._log(
            f"[ON_TRADE] oid={oid} direction={direction!r} offset={offset_str!r} "
            f"price={price} vol={vol}"
        )

        # 滑点
        slip = self._slip.on_fill(price, vol, direction)
        if slip != 0:
            self._log(f"[滑点] {slip:.1f}ticks")

        if direction == "buy" and is_open:
            old = self._own_pos
            new = old + vol
            # 加权均价
            if old == 0:
                self.avg_price = price
            else:
                self.avg_price = (self.avg_price * old + price * vol) / new
            self._own_pos = new
            self.state_map.own_pos = self._own_pos
            self.state_map.avg_price = self.avg_price
            self.state_map.entry_atr = self._entry_atr
            self.state_map.last_action = f"BUY {vol}手 @ {price}"
            self._log(
                f"[OPEN] own_pos {old}→{new} avg={self.avg_price:.2f} "
                f"entry_atr={self._entry_atr:.2f}"
            )
            self._pending = None
            self._save()
        elif direction == "sell" and not is_open:
            # 平仓
            entry = self.avg_price
            self._own_pos = max(0, self._own_pos - vol)
            self.state_map.own_pos = self._own_pos
            self.state_map.last_action = f"SELL {vol}手 @ {price}"
            pnl = (price - entry) * vol * self._multiplier
            self._perf.on_close(entry, price, vol, "long")
            self.state_map.perf = self._perf.format_report()
            self._log(
                f"[CLOSE] own_pos→{self._own_pos} pnl={pnl:.2f} "
                f"entry={entry:.2f} exit={price:.2f}"
            )
            if self._own_pos == 0:
                self.avg_price = 0.0
                self._entry_atr = 0.0
                self.state_map.avg_price = 0.0
                self.state_map.entry_atr = 0.0
                self.state_map.profit_target = 0.0
                self.state_map.hard_stop_line = 0.0
            self._pending = None
            self._save()

    def on_order(self, order: OrderData) -> None:
        super().on_order(order)

    def on_order_cancel(self, order: OrderData) -> None:
        oid = order.order_id
        cancel_vol = getattr(order, "cancel_volume", 0)
        self._log(f"[ON_ORDER_CANCEL] oid={oid} cancel_vol={cancel_vol}")
        self.order_id.discard(oid)
        self._om.unregister(oid)
        if self._pending and cancel_vol > 0:
            self._pending = None

    def on_error(self, error) -> None:
        throttle_on_error(self, error)
