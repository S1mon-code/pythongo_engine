"""
================================================================================
  AL_Long_M1_Test_Oscillator — 1分钟振子测试策略 (基于 TestAllFixes 模版)
================================================================================

  目的: 以最高频率 (M1 + sim_24h) 测试 UI / 订单流 / 日志 / 自管持仓过滤

  频率: M1 (每 1 分钟 bar 闭合触发)
  合约: SHFE al2607
  sim_24h: True (盘外也能接到模拟 tick, 不会陷入等待)

  信号 (振子): 12-bar 周期 (每 12 分钟完整跑一遍)
    target = [0, 1, 2, 3, 4, 5, 5, 4, 3, 2, 1, 0]
    依次触发 OPEN → ADD×4 → NO_ACT → REDUCE×4 → CLOSE → NO_ACT → 循环

  自管持仓 (同 V9/V10):
    - self._own_pos 内部计数
    - self._my_oids 过滤 on_trade, broker 其他仓完全不碰
    - max_lots = 5 (硬上限)

  止损 (放宽, 避免干扰振子):
    hard 5% / trail 3% / equity 20%

  图表:
    主图: MA20 + MA60 (均线)
    副图: target_lots (0-5) + cycle_pos (0-11)

  日志:
    self.output() → INFINIGO.writeLog → logs/StraLog.txt
    print(..., flush=True) → stdout (兜底, 若客户端也抓 stdout)
    关键点双写, 保证实时落盘
================================================================================
"""
import sys
import time
from datetime import datetime

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
from modules.risk import RiskManager
from modules.rollover import check_rollover
from modules.session_guard import SessionGuard
from modules.slippage import SlippageTracker
from modules.trading_day import get_trading_day


STRATEGY_NAME = "AL_Long_M1_Test_Oscillator"

# 振子参数
OSC_CYCLE_LEN = 12
OSC_TARGET_MAP = [0, 1, 2, 3, 4, 5, 5, 4, 3, 2, 1, 0]

# MA 周期 (主图)
MA_SHORT = 20
MA_LONG = 60

# 预热: 足够算 MA60
WARMUP = MA_LONG + 2

# 硬上限
MAX_LOTS = 5


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
    return 60


def oscillator_target(live_bar: int) -> tuple[int, int]:
    """振子: 返回 (target_lots, cycle_pos 0-11)."""
    cycle = live_bar % OSC_CYCLE_LEN
    return OSC_TARGET_MAP[cycle], cycle


def _sma(closes: np.ndarray, period: int) -> float:
    """简单移动均线, 数据不够返回 closes[-1]."""
    n = len(closes)
    if n == 0:
        return 0.0
    if n < period:
        return float(closes[-1])
    return float(np.mean(closes[-period:]))


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════


class Params(BaseParams):
    exchange: str = Field(default="SHFE", title="交易所代码")
    instrument_id: str = Field(default="al2607", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")
    max_lots: int = Field(default=MAX_LOTS, title="最大持仓(硬上限5)")
    capital: float = Field(default=1_000_000, title="配置资金")
    # 宽止损, 保证振子不被轻易打断
    hard_stop_pct: float = Field(default=5.0, title="硬止损(%)")
    trailing_pct: float = Field(default=3.0, title="移动止损(%)")
    equity_stop_pct: float = Field(default=20.0, title="权益止损(%)")
    flatten_minutes: int = Field(default=5, title="即将收盘(分钟)")
    sim_24h: bool = Field(default=True, title="24H模拟盘模式")


class State(BaseState):
    signal: float = Field(default=0.0, title="振子信号(0-1)")
    target_lots: int = Field(default=0, title="目标手")
    cycle_pos: int = Field(default=0, title="周期位置(0-11)")
    live_bar: int = Field(default=0, title="实盘bar计数")
    own_pos: int = Field(default=0, title="自管持仓")
    broker_pos: int = Field(default=0, title="账户总持仓")
    my_oids_n: int = Field(default=0, title="已发单累计")
    tick_count: int = Field(default=0, title="tick计数")
    bar_count: int = Field(default=0, title="bar计数")
    # UI 实时字段 (每 tick 都更新, 方便肉眼观察 UI 是否在动)
    last_price: float = Field(default=0.0, title="最新价")
    last_bid1: float = Field(default=0.0, title="买一价")
    last_ask1: float = Field(default=0.0, title="卖一价")
    spread_tick: int = Field(default=0, title="盘口价差(tick)")
    last_tick_time: str = Field(default="---", title="最后tick时间")
    heartbeat: int = Field(default=0, title="心跳(每tick+1)")
    ui_push_count: int = Field(default=0, title="UI推送次数")
    status_bar_updates: int = Field(default=0, title="状态栏刷新次数")
    ma20: float = Field(default=0.0, title="MA20")
    ma60: float = Field(default=0.0, title="MA60")
    avg_price: float = Field(default=0.0, title="均价")
    peak_price: float = Field(default=0.0, title="峰价")
    hard_line: float = Field(default=0.0, title="硬止损线")
    trail_line: float = Field(default=0.0, title="移损线")
    equity: float = Field(default=0.0, title="权益")
    drawdown: str = Field(default="---", title="回撤")
    trading_day: str = Field(default="", title="交易日")
    session: str = Field(default="---", title="交易时段")
    pending: str = Field(default="---", title="待执行")
    last_action: str = Field(default="---", title="上次操作")
    last_direction: str = Field(default="---", title="上次direction(DIAG)")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════


class AL_Long_M1_Test_Oscillator(BaseStrategy):
    """AL M1 振子测试 — 自管持仓, 12 bar 周期, sim_24h."""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        # 自管理持仓
        self._own_pos: int = 0
        self._my_oids: set = set()
        self.avg_price = 0.0
        self.peak_price = 0.0

        # pending / 挂单
        self._pending = None
        self._pending_target = None
        self._pending_reason = ""
        self.order_id = set()

        # 账户 / 风控
        self._investor_id = ""
        self._risk: RiskManager | None = None
        self._current_td = ""
        self._today_trades = []

        # 模块
        self._guard: SessionGuard | None = None
        self._slip: SlippageTracker | None = None
        self._hb: HeartbeatMonitor | None = None
        self._om = OrderMonitor()
        self._perf: PerformanceTracker | None = None
        self._pricer: AggressivePricer | None = None
        self._multiplier = 5

        # 指标缓存 (供 UI)
        self._ind_ma20 = 0.0
        self._ind_ma60 = 0.0
        self._ind_target = 0.0
        self._ind_cycle = 0.0

        # 诊断计数器
        self._tick_count = 0
        self._bar_count = 0             # callback 总次数 (含历史)
        self._live_bar_count = 0        # 实盘 bar 计数 (trading=True 的 callback)
        self._realtime_cb_count = 0
        self._last_session_state = None
        self._widget_err_count = 0
        self._widget_ok_count = 0

    # ══════════════════════════════════════════════════════════════════════
    #  日志封装 — 实时双写
    # ══════════════════════════════════════════════════════════════════════

    def _log(self, msg: str) -> None:
        """双写日志: output() → StraLog.txt (无限易) + print() → stdout."""
        self.output(msg)
        try:
            print(f"[{STRATEGY_NAME}] {msg}", flush=True)
            sys.stdout.flush()
        except Exception:
            pass

    @property
    def main_indicator_data(self):
        return {"MA20": self._ind_ma20, "MA60": self._ind_ma60}

    @property
    def sub_indicator_data(self):
        return {"target_lots": self._ind_target, "cycle_pos": self._ind_cycle}

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    def _current_bar_ts(self) -> int:
        sec = _freq_to_sec(self.params_map.kline_style)
        return int(time.time() // sec * sec)

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
            f"[ON_START] 模块初始化完成 multiplier={self._multiplier} "
            f"tick_size={get_tick_size(p.instrument_id)} sim_24h={p.sim_24h}"
        )

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self._log(f"[ON_START] KLineGenerator 创建, 开始 push_history_data...")
        self.kline_generator.push_history_data()
        producer = self.kline_generator.producer
        self._log(
            f"[ON_START] push_history 完成 producer_bars={len(producer.close)} "
            f"(需要 {WARMUP} 根预热)"
        )

        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id
        self._log(f"[ON_START] investor_id={self._investor_id}")

        self._risk = RiskManager(capital=p.capital)

        saved = load_state(STRATEGY_NAME)
        if saved:
            self._risk.load_state(saved)
            self._own_pos = int(saved.get("own_pos", 0))
            self._my_oids = set(saved.get("my_oids", []))
            self.avg_price = saved.get("avg_price", 0.0)
            self.peak_price = saved.get("peak_price", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._live_bar_count = int(saved.get("live_bar_count", 0))
            self._log(
                f"[ON_START 恢复] own_pos={self._own_pos} avg={self.avg_price:.1f} "
                f"peak={self.peak_price:.1f} my_oids={len(self._my_oids)} "
                f"live_bar={self._live_bar_count}"
            )

        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance, acct.position_profit)
            self._log(
                f"[ON_START 账户] balance={acct.balance:.0f} "
                f"available={acct.available:.0f} "
                f"position_profit={acct.position_profit:.0f}"
            )

        pos = self.get_position(p.instrument_id)
        broker_pos = pos.net_position if pos else 0
        self.state_map.own_pos = self._own_pos
        self.state_map.broker_pos = broker_pos
        self._log(f"[ON_START 持仓] own_pos={self._own_pos} broker_pos={broker_pos}")

        if self._own_pos == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        level, days = check_rollover(p.instrument_id)
        if level:
            feishu("rollover", p.instrument_id, f"**换月**: 距交割月**{days}天**")

        self._log(
            f"[ON_START] 调用 super().on_start() "
            f"(trading=True + sub_market_data + load_data_signal)"
        )
        super().on_start()

        self._log(
            f"=== 启动完成 === | {p.instrument_id} {p.kline_style} | "
            f"max_lots={p.max_lots} | sim_24h={p.sim_24h} | "
            f"own_pos={self._own_pos} broker_pos={broker_pos} | "
            f"session={self._guard.get_status()} should_trade={self._guard.should_trade()}"
        )
        self._log(
            "=== UI 测试清单 === (启动后请对照检查):\n"
            "  1. 状态栏: 含 heartbeat / last_price / ma20 / ma60 / "
            "tick_count / bar_count / own_pos 等实时字段\n"
            "  2. 主图: MA20 + MA60 两条线 (随价格变化)\n"
            "  3. 副图: target_lots (0-5 锯齿) + cycle_pos (0-11)\n"
            "  4. 买/卖箭头: 每次实际发单会在 K 线上标记\n"
            "  5. 心跳: heartbeat 字段每 tick +1, 到 1000 归零 — 肉眼观察UI是否在刷新\n"
            "  6. status_bar_updates: 每 10 tick 调 update_status_bar, 确认 state_map 传播\n"
            "  7. ui_push_count: 每次 widget.recv_kline 成功的次数"
        )
        feishu("start", p.instrument_id,
               f"**[TEST] {STRATEGY_NAME} 启动**\n"
               f"合约 {p.instrument_id} {p.kline_style}\n"
               f"振子周期 12 bar ({OSC_TARGET_MAP})\n"
               f"自管持仓: {self._own_pos}手 (账户总: {broker_pos})\n"
               f"sim_24h={p.sim_24h} | max_lots={p.max_lots}\n"
               f"宽止损 hard={p.hard_stop_pct}% trail={p.trailing_pct}%")

    def on_stop(self):
        self._save()
        self._log(
            f"[ON_STOP] tick_count={self._tick_count} bar_count={self._bar_count} "
            f"live_bar={self._live_bar_count} own_pos={self._own_pos} "
            f"my_oids={len(self._my_oids)}"
        )
        feishu("shutdown", self.params_map.instrument_id,
               f"**[TEST] 停止** {STRATEGY_NAME}\n"
               f"own_pos: {self._own_pos}手 | live_bar: {self._live_bar_count}\n"
               f"tick_count: {self._tick_count}\n"
               f"{self._slip.format_report() if self._slip else ''}")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)

        self._tick_count += 1
        self.state_map.tick_count = self._tick_count
        self.state_map.heartbeat = self._tick_count % 1000   # 方便 UI 肉眼观察在跳

        # ── UI 实时字段 (每 tick 都更新) ──
        self.state_map.last_price = float(tick.last_price)
        self.state_map.last_bid1 = float(tick.bid_price1)
        self.state_map.last_ask1 = float(tick.ask_price1)
        try:
            tick_size = get_tick_size(self.params_map.instrument_id) or 0.01
            if tick.ask_price1 > 0 and tick.bid_price1 > 0:
                self.state_map.spread_tick = int(round(
                    (tick.ask_price1 - tick.bid_price1) / tick_size
                ))
        except Exception:
            pass
        try:
            self.state_map.last_tick_time = tick.datetime.strftime("%H:%M:%S")
        except Exception:
            self.state_map.last_tick_time = str(tick.datetime)

        # 前 5 个 tick + 每 500 个打一次
        if self._tick_count <= 5 or self._tick_count % 500 == 0:
            sess = self._guard.get_status() if self._guard else "?"
            should = self._guard.should_trade() if self._guard else True
            self._log(
                f"[TICK #{self._tick_count}] "
                f"inst={tick.instrument_id} last={tick.last_price} "
                f"bid1={tick.bid_price1} ask1={tick.ask_price1} "
                f"vol={tick.volume} dt={tick.datetime} | "
                f"session={sess} should_trade={should} trading={self.trading}"
            )

        # ── 状态栏刷新 (每 10 tick 一次, 保证 UI 状态栏可见更新) ──
        if self._tick_count % 10 == 0:
            try:
                self.update_status_bar()
                self.state_map.status_bar_updates = (
                    self.state_map.status_bar_updates + 1
                )
                if self._tick_count <= 50 or self._tick_count % 1000 == 0:
                    self._log(
                        f"[UI_STATUS] 第 {self.state_map.status_bar_updates} 次刷新 "
                        f"state_map (tick #{self._tick_count})"
                    )
            except Exception as e:
                self._log(f"[UI_STATUS 异常] {type(e).__name__}: {e}")

        self.kline_generator.tick_to_kline(tick)

        if self._pricer is not None:
            try:
                self._pricer.update(tick)
            except Exception as e:
                self._log(f"[pricer异常] {type(e).__name__}: {e}")

        try:
            self._on_tick_stops(tick)
        except Exception as e:
            self._log(f"[stops异常] {type(e).__name__}: {e}")

        try:
            self._on_tick_aux(tick)
        except Exception as e:
            self._log(f"[on_tick_aux异常] {type(e).__name__}: {e}")
            feishu("error", self.params_map.instrument_id,
                   f"**on_tick异常**\n{type(e).__name__}: {e}")

    def _on_tick_stops(self, tick: TickData):
        if not self.trading:
            return
        if self._guard is not None and not self._guard.should_trade():
            return
        if self._pending is not None:
            return
        if self._own_pos <= 0:
            return

        p = self.params_map
        price = tick.last_price

        self._risk.update_peak_trough_tick(price, self._own_pos)
        self.peak_price = self._risk.peak_price

        action, reason = self._risk.check_hard_stop_tick(
            price=price, avg_price=self.avg_price,
            net_pos=self._own_pos, hard_stop_pct=p.hard_stop_pct,
        )
        if action:
            self._log(f"[{action}][TICK] {reason}")
            self._exec_stop_at_tick(price, action, reason)
            return

        action, reason = self._risk.check_trail_minutely(
            price=price, now=datetime.now(),
            net_pos=self._own_pos, trailing_pct=p.trailing_pct,
        )
        if action:
            self._log(f"[{action}][M1] {reason}")
            self._exec_stop_at_tick(price, action, reason)

    def _on_tick_aux(self, tick: TickData):
        p = self.params_map

        if self._guard is not None:
            cur = self._guard.should_trade()
            if cur != self._last_session_state:
                self._log(
                    f"[SESSION_CHANGE] should_trade "
                    f"{self._last_session_state} → {cur} | "
                    f"status={self._guard.get_status()}"
                )
                self._last_session_state = cur

        if (self._guard is not None and self._guard.should_trade()
                and self._pricer is not None):
            to_escalate = self._om.check_escalation()
            for oid, next_urgency, info in to_escalate:
                self._resubmit_escalated(oid, next_urgency, info)

        td = get_trading_day()
        if td != self._current_td and self._current_td:
            acct = self._get_account()
            if acct:
                self._risk.on_day_change(acct.balance, acct.position_profit)
            self._perf.on_day_change()
            self._today_trades = []
            self._current_td = td
            self.state_map.trading_day = td
            self._save()
            self._log(f"[新交易日] {td}")
        if not self._current_td:
            self._current_td = td
            self.state_map.trading_day = td

        for atype, msg in self._hb.check(p.instrument_id):
            if atype == "no_tick":
                feishu("no_tick", p.instrument_id, msg)

        self.state_map.session = self._guard.get_status()

    # ══════════════════════════════════════════════════════════════════════
    #  定价 / escalator
    # ══════════════════════════════════════════════════════════════════════

    def _aggressive_price(self, price, direction, urgency: str = "normal"):
        if self._pricer is None or self._pricer.last == 0:
            return price
        return self._pricer.price(direction, urgency)

    def _resubmit_escalated(self, old_oid, next_urgency: str, info: dict) -> None:
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
            self._log(f"[ESCALATE] oid={old_oid} → {next_urgency}: 重发 None")
            return
        self.order_id.add(new_oid)
        self._my_oids.add(new_oid)
        self._om.on_send(new_oid, vol, new_price,
                         urgency=next_urgency, direction=direction, kind=kind)
        self._log(
            f"[ESCALATE] {old_oid} → {new_oid} | {direction} {vol}手 "
            f"@ {new_price:.1f} | {info.get('urgency')} → {next_urgency}"
        )

    # ══════════════════════════════════════════════════════════════════════
    #  K线回调
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        self._bar_count += 1
        if self._bar_count <= 3 or self._bar_count % 20 == 0:
            producer = self.kline_generator.producer if self.kline_generator else None
            n = len(producer.close) if producer else 0
            self._log(
                f"[BAR #{self._bar_count}] dt={kline.datetime} "
                f"O={kline.open} H={kline.high} L={kline.low} C={kline.close} "
                f"V={kline.volume} | producer_n={n} trading={self.trading}"
            )
        try:
            self._on_bar(kline)
        except Exception as e:
            self._log(f"[callback异常] {type(e).__name__}: {e}")

    def real_time_callback(self, kline: KLineData):
        self._realtime_cb_count += 1
        if self._realtime_cb_count == 1 or self._realtime_cb_count % 200 == 0:
            self._log(
                f"[RT_CB #{self._realtime_cb_count}] "
                f"last_close={kline.close} dt={kline.datetime}"
            )
        self._push_widget(kline)

    def _refresh_indicators(self) -> None:
        producer = self.kline_generator.producer
        n = len(producer.close)
        if n < 2:
            return
        closes = np.asarray(producer.close, dtype=np.float64)
        self._ind_ma20 = _sma(closes, MA_SHORT)
        self._ind_ma60 = _sma(closes, MA_LONG)
        # 同步到 state_map 让状态栏看得到
        self.state_map.ma20 = round(self._ind_ma20, 1)
        self.state_map.ma60 = round(self._ind_ma60, 1)

    def _on_bar(self, kline: KLineData):
        p = self.params_map
        signal_price = 0.0

        try:
            self._refresh_indicators()
        except Exception as e:
            self._log(f"[refresh_indicators 异常] {type(e).__name__}: {e}")

        # 历史回放
        if not self.trading:
            if self._bar_count <= 3 or self._bar_count % 20 == 0:
                self._log(f"[ON_BAR 历史] bar#{self._bar_count} trading=False")
            self._push_widget(kline)
            return

        # 非交易时段 (sim_24h=True 时不会进这里)
        if self._guard is not None and not self._guard.should_trade():
            self._log(
                f"[ON_BAR 非交易时段] bar#{self._bar_count} "
                f"session={self._guard.get_status()} close={kline.close}"
            )
            self.state_map.session = self._guard.get_status()
            self._push_widget(kline)
            self.update_status_bar()
            return

        self._log(
            f"[ON_BAR 实盘] bar#{self._bar_count} dt={kline.datetime} "
            f"close={kline.close} own_pos={self._own_pos} pending={self._pending}"
        )

        # 撤本策略的挂单
        n_cancel = 0
        for oid in list(self.order_id):
            self.cancel_order(oid)
            n_cancel += 1
        if n_cancel > 0:
            self._log(f"[ON_BAR] bar 开头撤 {n_cancel} 个挂单")
        for oid in self._om.check_timeouts(self.cancel_order):
            self._log(f"[超时撤单] {oid}")

        # 残留 pending (如 tick 级止损没发成功)
        if self._pending is not None:
            self._log(f"[ON_BAR] 执行残留 pending={self._pending} reason={self._pending_reason}")
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # 预热检查
        producer = self.kline_generator.producer
        if len(producer.close) < WARMUP:
            self._log(
                f"[ON_BAR 预热] producer_bars={len(producer.close)} "
                f"需要 {WARMUP} 根, 还差 {WARMUP - len(producer.close)}"
            )
            self._push_widget(kline)
            return

        # ──── 振子: live_bar 递增, 算 target ────
        self._live_bar_count += 1
        target, cycle = oscillator_target(self._live_bar_count)
        target = min(target, p.max_lots, MAX_LOTS)
        signal_raw = target / max(1, MAX_LOTS)

        self._ind_target = float(target)
        self._ind_cycle = float(cycle)

        self.state_map.signal = round(signal_raw, 3)
        self.state_map.target_lots = target
        self.state_map.cycle_pos = cycle
        self.state_map.live_bar = self._live_bar_count
        self.state_map.bar_count = self._bar_count

        self._log(
            f"[OSCILLATOR] live_bar={self._live_bar_count} cycle={cycle}/{OSC_CYCLE_LEN-1} "
            f"target={target} | MA20={self._ind_ma20:.1f} MA60={self._ind_ma60:.1f} "
            f"close={kline.close}"
        )

        self.state_map.own_pos = self._own_pos
        bpos = self.get_position(p.instrument_id)
        self.state_map.broker_pos = bpos.net_position if bpos else 0

        # 持仓追踪
        close = float(kline.close)
        if self._own_pos == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0
        else:
            self._risk.update_peak_trough_tick(close, self._own_pos)
            self.peak_price = self._risk.peak_price
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.peak_price = round(self.peak_price, 1)
        self.state_map.hard_line = (
            round(self.avg_price * (1 - p.hard_stop_pct / 100), 1)
            if self._own_pos > 0 else 0.0
        )
        self.state_map.trail_line = (
            round(self.peak_price * (1 - p.trailing_pct / 100), 1)
            if self._own_pos > 0 else 0.0
        )

        # 权益
        acct = self._get_account()
        if acct:
            self._risk.update(acct.balance)
            self.state_map.equity = round(acct.balance, 0)
            self.state_map.drawdown = f"{self._risk.drawdown_pct:.2%}"

        self._log(
            f"[POS_DECISION] own_pos={self._own_pos} → target={target} "
            f"broker_pos={self.state_map.broker_pos} cycle={cycle} "
            f"(delta={target - self._own_pos})"
        )

        # ──── 信号 → 动作 ────
        if target == self._own_pos:
            self._log(f"[NO_ACTION] target == own_pos = {target}, 无需调仓")
        elif self._own_pos == 0 and target > 0:
            self._pending = "OPEN"
            self._pending_target = target
            self._pending_reason = f"[振子] live_bar={self._live_bar_count} cycle={cycle} target={target}"
            self._log(f"[PENDING] OPEN target={target}")
        elif target == 0 and self._own_pos > 0:
            self._pending = "CLOSE"
            self._pending_target = 0
            self._pending_reason = f"[振子] live_bar={self._live_bar_count} cycle={cycle} target=0"
            self._log(f"[PENDING] CLOSE own_pos={self._own_pos}")
        elif target > self._own_pos:
            self._pending = "ADD"
            self._pending_target = target
            self._pending_reason = f"[振子] live_bar={self._live_bar_count} cycle={cycle} target={target}"
            self._log(f"[PENDING] ADD own_pos={self._own_pos}→{target}")
        elif target < self._own_pos:
            self._pending = "REDUCE"
            self._pending_target = target
            self._pending_reason = f"[振子] live_bar={self._live_bar_count} cycle={cycle} target={target}"
            self._log(f"[PENDING] REDUCE own_pos={self._own_pos}→{target}")

        if self._pending is not None:
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""

        self.state_map.pending = self._pending or "---"
        self.state_map.my_oids_n = len(self._my_oids)
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ══════════════════════════════════════════════════════════════════════
    #  执行
    # ══════════════════════════════════════════════════════════════════════

    def _execute(self, kline: KLineData, action: str) -> float:
        p = self.params_map
        price = kline.close
        self._log(
            f"[EXECUTE] action={action} price={price} own_pos={self._own_pos} "
            f"target={self._pending_target} reason={self._pending_reason}"
        )
        if self._guard is not None and not self._guard.should_trade():
            self._log(f"[执行跳过] 非交易时段 {action}")
            return 0.0

        if action == "OPEN":
            return self._exec_open(price)
        if action == "ADD":
            return self._exec_add(price)
        if action == "REDUCE":
            return self._exec_reduce(price)
        if action in ("CLOSE", "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP",
                      "CIRCUIT", "DAILY_STOP", "FLATTEN"):
            return self._exec_close(kline, action)
        self._log(f"[EXECUTE] 未知 action={action}")
        return 0.0

    def _exec_open(self, price: float) -> float:
        p = self.params_map
        target = min(self._pending_target or 1, MAX_LOTS)
        vol = max(1, target)
        if self._own_pos > 0:
            return self._exec_add(price)

        acct = self._get_account()
        if acct:
            need = price * self._multiplier * vol * 0.15
            limit = acct.available * 0.6
            self._log(
                f"[EXEC_OPEN 保证金] need={need:,.0f} "
                f"available={acct.available:,.0f} limit(60%)={limit:,.0f}"
            )
            if need > limit:
                self._log(f"[保证金不足] OPEN {vol}手")
                feishu("error", p.instrument_id, f"**保证金不足** OPEN {vol}手")
                return 0.0

        self._slip.set_signal_price(price)
        buy_price = self._aggressive_price(price, "buy", urgency="passive")
        self._log(f"[EXEC_OPEN] send_order buy {vol}手 @ {buy_price} (passive)")
        oid = self.send_order(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=buy_price, order_direction="buy",
        )
        self._log(f"[EXEC_OPEN] send_order 返回 oid={oid}")
        if oid is None:
            self._log(f"[OPEN] 发单失败 (oid=None)")
            return 0.0
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, buy_price,
                         urgency="passive", direction="buy", kind="open")
        self.state_map.last_action = f"建仓{vol}手"
        feishu("open", p.instrument_id,
               f"**[TEST] 建仓** {vol}手 @ {buy_price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"自管: 0 → {vol}手")
        return price

    def _exec_add(self, price: float) -> float:
        p = self.params_map
        target = min(self._pending_target or (self._own_pos + 1), MAX_LOTS)
        vol = max(1, target - self._own_pos)
        if vol <= 0:
            return 0.0
        acct = self._get_account()
        if acct and price * self._multiplier * vol * 0.15 > acct.available * 0.6:
            self._log("[加仓保证金不足]")
            return 0.0
        self._slip.set_signal_price(price)
        buy_price = self._aggressive_price(price, "buy", urgency="passive")
        self._log(f"[EXEC_ADD] send_order buy {vol}手 @ {buy_price} (passive)")
        oid = self.send_order(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=buy_price, order_direction="buy",
        )
        self._log(f"[EXEC_ADD] send_order 返回 oid={oid}")
        if oid is None:
            return 0.0
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, buy_price,
                         urgency="passive", direction="buy", kind="open")
        self.state_map.last_action = f"加仓{vol}手"
        feishu("add", p.instrument_id,
               f"**[TEST] 加仓** {vol}手 @ {buy_price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"自管: {self._own_pos} → {self._own_pos + vol}手")
        return price

    def _exec_reduce(self, price: float) -> float:
        p = self.params_map
        target = self._pending_target if self._pending_target is not None else self._own_pos // 2
        target = max(0, target)
        vol = self._own_pos - target
        if vol <= 0 or self._own_pos <= 0:
            return 0.0
        self._slip.set_signal_price(price)
        sell_price = self._aggressive_price(price, "sell", urgency="normal")
        self._log(f"[EXEC_REDUCE] auto_close sell {vol}手 @ {sell_price} (normal)")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=sell_price, order_direction="sell",
        )
        self._log(f"[EXEC_REDUCE] 返回 oid={oid}")
        if oid is None:
            return 0.0
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, sell_price,
                         urgency="normal", direction="sell", kind="reduce")
        self.state_map.last_action = f"减仓{vol}手"
        feishu("reduce", p.instrument_id,
               f"**[TEST] 减仓** {vol}手 @ {sell_price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"自管: {self._own_pos} → {target}手")
        return -price

    def _exec_close(self, kline: KLineData, action: str) -> float:
        labels = {
            "CLOSE": "信号平仓", "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损",
            "EQUITY_STOP": "权益止损", "CIRCUIT": "熔断", "DAILY_STOP": "单日止损",
            "FLATTEN": "即将收盘清仓",
        }
        label = labels.get(action, action)
        p = self.params_map
        price = kline.close
        vol = self._own_pos
        if vol <= 0:
            return 0.0
        self._slip.set_signal_price(price)
        urgency = "normal" if action == "CLOSE" else (
            "critical" if action in ("EQUITY_STOP", "CIRCUIT", "DAILY_STOP", "FLATTEN") else "urgent"
        )
        sell_price = self._aggressive_price(price, "sell", urgency=urgency)
        self._log(f"[EXEC_CLOSE] {label} auto_close sell {vol}手 @ {sell_price} ({urgency})")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=sell_price, order_direction="sell",
        )
        self._log(f"[EXEC_CLOSE] 返回 oid={oid}")
        if oid is None:
            feishu("error", p.instrument_id, f"**{label}发单失败** {vol}手")
            return 0.0
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, sell_price,
                         urgency=urgency, direction="sell", kind="close")
        pnl_pct = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
        abs_pnl = self._perf.on_close(self.avg_price, price, vol)
        self.state_map.last_action = f"{label} {pnl_pct:+.2f}%"
        feishu(action.lower(), p.instrument_id,
               f"**[TEST] {label}** {vol}手 @ {sell_price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})\n"
               f"自管: {self._own_pos} → 0手")
        return -price

    def _exec_stop_at_tick(self, price: float, action: str, reason: str) -> None:
        p = self.params_map
        if self._guard is not None and not self._guard.should_trade():
            return
        vol = self._own_pos
        if vol <= 0:
            return

        for oid in list(self.order_id):
            self.cancel_order(oid)

        self._pending_reason = reason
        self._slip.set_signal_price(price)
        urgency = "critical" if action in (
            "EQUITY_STOP", "CIRCUIT", "DAILY_STOP", "FLATTEN"
        ) else "urgent"
        sell_price = self._aggressive_price(price, "sell", urgency=urgency)
        self._log(f"[EXEC_STOP] {action} auto_close sell {vol}手 @ {sell_price} ({urgency})")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=sell_price, order_direction="sell",
        )
        if oid is None:
            self._log(f"[TICK_STOP] auto_close None, 保留 pending={action}")
            self._pending = action
            return
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, sell_price,
                         urgency=urgency, direction="sell", kind="close")
        labels = {"HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损"}
        label = labels.get(action, action)
        pnl_pct = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
        abs_pnl = self._perf.on_close(self.avg_price, price, vol)
        self.state_map.last_action = f"{label}[TICK] {pnl_pct:+.2f}%"
        feishu(action.lower(), p.instrument_id,
               f"**[TEST] {label}** (tick触发) {vol}手 @ {price:,.1f}\n"
               f"逻辑: {reason}\n盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})")
        self._pending = action
        self._risk.peak_price = 0.0
        self._risk.trough_price = 0.0
        self._risk._last_trail_minute = None
        self._save()

    # ══════════════════════════════════════════════════════════════════════
    #  存档 / UI
    # ══════════════════════════════════════════════════════════════════════

    def _save(self):
        state = {
            "own_pos": self._own_pos,
            "my_oids": list(self._my_oids)[-500:],
            "avg_price": self.avg_price,
            "peak_price": self.peak_price,
            "trading_day": self._current_td,
            "live_bar_count": self._live_bar_count,
        }
        if self._risk is not None:
            state.update(self._risk.get_state())
        save_state(state, name=STRATEGY_NAME)

    def _push_widget(self, kline, sp=0.0):
        if self.widget is None:
            self._widget_err_count += 1
            if self._widget_err_count <= 3 or self._widget_err_count % 500 == 0:
                self._log(
                    f"[WIDGET] self.widget=None "
                    f"(累计 {self._widget_err_count} 次). "
                    f"排查: 1)是否无限易环境 2)on_init 是否跑完 "
                    f"3)qt_gui_support 是否就绪"
                )
            return

        # 合并 payload + 类型校验 (防止 NaN/None 污染图表)
        main = self.main_indicator_data
        sub = self.sub_indicator_data

        payload = {"kline": kline, "signal_price": float(sp)}
        for name, val in {**main, **sub}.items():
            if val is None or (isinstance(val, float) and np.isnan(val)):
                val = 0.0
            try:
                payload[name] = float(val)
            except (TypeError, ValueError):
                payload[name] = 0.0

        try:
            self.widget.recv_kline(payload)
            self._widget_ok_count += 1
            self.state_map.ui_push_count = self._widget_ok_count

            if self._widget_ok_count == 1:
                self._log(
                    f"[WIDGET] 首次推送成功! payload keys={list(payload.keys())} "
                    f"values=[{', '.join(f'{k}={payload[k]}' for k in payload if k != 'kline')}]"
                )
            elif self._widget_ok_count <= 5:
                self._log(
                    f"[WIDGET #{self._widget_ok_count}] "
                    f"推送 signal_price={sp} "
                    f"MA20={payload.get('MA20', 0):.1f} MA60={payload.get('MA60', 0):.1f} "
                    f"target_lots={payload.get('target_lots', 0)} "
                    f"cycle_pos={payload.get('cycle_pos', 0)}"
                )
            elif self._widget_ok_count % 500 == 0:
                self._log(f"[WIDGET #{self._widget_ok_count}] 累计推送 OK")
        except Exception as e:
            self._widget_err_count += 1
            if self._widget_err_count <= 3 or self._widget_err_count % 500 == 0:
                self._log(
                    f"[WIDGET] recv_kline 异常: {type(e).__name__}: {e} "
                    f"payload keys={list(payload.keys())}"
                )

    # ══════════════════════════════════════════════════════════════════════
    #  回调
    # ══════════════════════════════════════════════════════════════════════

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        oid = trade.order_id
        self.order_id.discard(oid)
        self._om.on_fill(oid)

        self._log(
            f"[ON_TRADE] oid={oid} direction={trade.direction!r} "
            f"offset={trade.offset!r} price={trade.price} vol={trade.volume}"
        )

        if oid not in self._my_oids:
            self._log(f"[ON_TRADE] oid={oid} 非本策略, 跳过 (vol={trade.volume})")
            return

        raw = str(trade.direction).lower()
        direction = "buy" if raw in ("buy", "0", "买") else "sell"
        self.state_map.last_direction = f"{trade.direction!r}→{direction}"

        slip = self._slip.on_fill(trade.price, trade.volume, direction)
        if slip != 0:
            self._log(f"[滑点] {slip:.1f}ticks")

        if direction == "buy":
            old = self._own_pos
            new = old + trade.volume
            if new > MAX_LOTS:
                self._log(f"[WARN] own_pos={new} > MAX_LOTS, 截断")
                new = MAX_LOTS
            if old > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old + trade.price * trade.volume) / new
            else:
                self.avg_price = trade.price
            if trade.price > self.peak_price or self.peak_price == 0:
                self.peak_price = trade.price
            self._own_pos = new
            self._log(
                f"[FILL BUY] {trade.volume}手 @ {trade.price:.1f} "
                f"own_pos {old}→{new} avg={self.avg_price:.1f}"
            )
        else:
            old = self._own_pos
            new = max(0, old - trade.volume)
            self._own_pos = new
            if new == 0:
                self.avg_price = 0.0
                self.peak_price = 0.0
            self._log(
                f"[FILL SELL] {trade.volume}手 @ {trade.price:.1f} "
                f"own_pos {old}→{new}"
            )

        self.state_map.own_pos = self._own_pos
        self.state_map.my_oids_n = len(self._my_oids)
        self._save()
        self.update_status_bar()

    def on_order(self, order: OrderData):
        mine = "(本策略)" if order.order_id in self._my_oids else "(非本策略)"
        self._log(
            f"[ON_ORDER] oid={order.order_id} status={order.status} "
            f"direction={order.direction} offset={order.offset} "
            f"price={order.price} total={order.total_volume} "
            f"traded={order.traded_volume} cancel={order.cancel_volume} {mine}"
        )
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        mine = "(本策略)" if order.order_id in self._my_oids else "(非本策略)"
        self._log(
            f"[ON_ORDER_CANCEL] oid={order.order_id} "
            f"cancel_volume={order.cancel_volume} {mine}"
        )
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)
        self._om.on_cancel(order.order_id)

    def on_error(self, error):
        self._log(f"[ON_ERROR] {error}")
        feishu("error", self.params_map.instrument_id, f"**[TEST] 异常**: {error}")
        throttle_on_error(self, error)
