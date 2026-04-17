"""
================================================================================
  TestScaledEntry — 专门测试 ScaledEntryExecutor 的实盘/仿真测试策略
================================================================================

  目的: 验证 ScaledEntryExecutor + RollingVWAP + AggressivePricer 在 Windows
       实盘环境下的完整行为 (BOTTOM → OPPORTUNISTIC → FORCE → COMPLETE),
       以及多空方向切换时的 close → open 衔接。

  策略逻辑 (极简):
    - Bar 1, 3, 5, ... (奇数): 目标 +10 手 (开多)
    - Bar 2, 4, 6, ... (偶数): 目标 -10 手 (开空)
    - 每 bar 收盘时决策: 若持仓反向先 close_all 立即平, 同 bar 内起 OPEN

  观察点:
    1. 首 tick `[AggressivePricer][schema]` 字段 dump
    2. `[ENTRY]` 挂单日志 (urgency + state)
    3. 飞书 `ENTRY BOTTOM / OPP 首笔 / FORCE / BOTTOM OVERDUE / REJECTED`
    4. state_map.entry_progress 显示
    5. 方向切换时 close+open 的时序
    6. `_unknown_oid_count` 应该 ≤ 方向切换次数 (每次 close 计 1)

  部署: 和其他 src/test/*.py 一致, 放 Windows pyStrategy/self_strategy/
================================================================================
"""
import time
from datetime import datetime

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.contract_info import get_multiplier, get_tick_size
from modules.execution import EntryParams, EntryState, ExecAction, ScaledEntryExecutor
from modules.feishu import feishu
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.persistence import save_state, load_state
from modules.pricing import AggressivePricer
from modules.risk import RiskManager
from modules.rolling_vwap import RollingVWAP
from modules.session_guard import SessionGuard
from modules.slippage import SlippageTracker
from modules.trading_day import get_trading_day

STRATEGY_NAME = "TestScaledEntry"


def _freq_to_sec(kline_style) -> int:
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
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="SHFE", title="交易所")
    instrument_id: str = Field(default="al2607", title="合约")
    kline_style: str = Field(default="H1", title="K线周期")
    capital: float = Field(default=1_000_000, title="名义本金")

    max_lots: int = Field(default=10, title="单方向目标手数")
    bottom_lots: int = Field(default=2, title="底仓手数")

    hard_stop_pct: float = Field(default=1.0, title="硬止损(%)")
    trailing_pct: float = Field(default=0.6, title="移动止损(%)")

    flatten_minutes: int = Field(default=5, title="距收盘提前清仓分钟")
    sim_24h: bool = Field(default=False, title="24h仿真")


class State(BaseState):
    bar_idx: int = Field(default=0, title="已处理bar数")
    target: int = Field(default=0, title="本bar目标(±10)")
    net_pos: int = Field(default=0, title="当前持仓(带符号)")
    entry_progress: str = Field(default="---", title="入场进度")
    last_action: str = Field(default="---", title="上次操作")
    trading_day: str = Field(default="", title="交易日")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

class TestScaledEntry(BaseStrategy):
    """交替开多开空的 ScaledEntryExecutor 测试策略"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        # 持仓状态
        self.avg_price = 0.0
        self.peak_price = 0.0        # 多头峰
        self.trough_price = 0.0      # 空头谷
        self._pending = None
        self._pending_target = None
        self._pending_direction = None
        self._pending_reason = ""
        self.order_id = set()

        # 模块
        self._guard = None
        self._slip = None
        self._hb = None
        self._om = OrderMonitor()
        self._perf = None
        self._pricer: AggressivePricer | None = None
        self._risk = None
        self._rvwap: RollingVWAP | None = None
        self._entry: ScaledEntryExecutor | None = None

        # 其他
        self._investor_id = ""
        self._multiplier = 5
        self._current_td = ""
        self._bar_idx = 0
        self._unknown_oid_count = 0

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
        self._rvwap = RollingVWAP(window_seconds=1800)
        self._entry = ScaledEntryExecutor(EntryParams(bottom_lots=p.bottom_lots))

        self._guard = SessionGuard(p.instrument_id, p.flatten_minutes, sim_24h=p.sim_24h)
        self._slip = SlippageTracker(p.instrument_id)
        self._hb = HeartbeatMonitor(p.instrument_id)
        self._perf = PerformanceTracker(p.instrument_id)

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange, instrument_id=p.instrument_id,
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
            self.avg_price = saved.get("avg_price", 0.0)
            self.peak_price = saved.get("peak_price", 0.0)
            self.trough_price = saved.get("trough_price", 0.0)
            self._bar_idx = saved.get("bar_idx", 0)
            self._current_td = saved.get("trading_day", "")

        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance)

        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual

        self.output(f"[启动] {STRATEGY_NAME} 合约={p.instrument_id} 乘数={self._multiplier} 持仓={actual}")
        feishu("start", p.instrument_id,
               f"**测试策略启动** {STRATEGY_NAME}\n"
               f"合约: {p.instrument_id} | max_lots: {p.max_lots}\n"
               f"策略逻辑: 奇 bar 开多 {p.max_lots}, 偶 bar 开空 {p.max_lots}\n"
               f"当前持仓: {actual}手 | bar_idx 已处理: {self._bar_idx}")
        super().on_start()

    def on_stop(self):
        self._save()
        feishu("shutdown", self.params_map.instrument_id,
               f"**测试策略停止**\n持仓: {self.state_map.net_pos}手 | "
               f"已处理 {self._bar_idx} bars\n"
               f"unknown_oid_count={self._unknown_oid_count}")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)

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
            self._drive_entry(tick)
        except Exception as e:
            self.output(f"[entry异常] {type(e).__name__}: {e}")

        try:
            self._on_tick_aux(tick)
        except Exception as e:
            self.output(f"[aux异常] {type(e).__name__}: {e}")

    def _drive_entry(self, tick: TickData) -> None:
        if self._entry is None or self._rvwap is None or self._pricer is None:
            return
        if not self.trading or (self._guard is not None and not self._guard.should_trade()):
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
            forecast=10.0,  # 测试里信号强度写死最大, 体现 urgency 时间压力为主
            current_position=net_pos,
        )
        for a in actions:
            self._apply_entry_action(a)

        self.state_map.net_pos = net_pos
        self.state_map.entry_progress = self._entry.progress_str()

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

    def _on_tick_aux(self, tick: TickData):
        td = get_trading_day()
        if td != self._current_td and self._current_td:
            acct = self._get_account()
            if acct:
                self._risk.on_day_change(acct.balance)
            self._perf.on_day_change()
            self._current_td = td
            self.state_map.trading_day = td
            self._save()
            self.output(f"[新交易日] {td}")
        if not self._current_td:
            self._current_td = td
            self.state_map.trading_day = td

        for atype, msg in self._hb.check(self.params_map.instrument_id):
            if atype == "no_tick":
                feishu("no_tick", self.params_map.instrument_id, msg)

    # ══════════════════════════════════════════════════════════════════════
    #  Bar 回调 — 核心 alternating 逻辑
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        try:
            self._on_bar(kline)
        except Exception as e:
            self.output(f"[callback异常] {type(e).__name__}: {e}")

    def real_time_callback(self, kline: KLineData):
        pass

    def _on_bar(self, kline: KLineData):
        if not self.trading:
            return

        if self._guard is not None and not self._guard.should_trade():
            self.output(f"[bar跳过] 非交易时段")
            return

        # 撤老挂单 (同步 executor)
        for oid in list(self.order_id):
            self.cancel_order(oid)
            if self._entry is not None:
                self._entry.register_cancelled(oid)

        p = self.params_map
        self._bar_idx += 1
        target_signed = p.max_lots if self._bar_idx % 2 == 1 else -p.max_lots
        target_dir = "buy" if target_signed > 0 else "sell"

        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0

        self.state_map.bar_idx = self._bar_idx
        self.state_map.target = target_signed
        self.state_map.net_pos = current

        self.output(
            f"[BAR {self._bar_idx}] 目标={target_signed} 当前持仓={current} close={kline.close:.1f}"
        )

        # Case 1: 已经到位, 不动
        if current == target_signed:
            self.output(f"[BAR {self._bar_idx}] 已达目标 {target_signed}手, 无动作")
            self._save()
            return

        # Case 2: 持仓反向 — 先立即平掉所有反向仓位
        if (target_dir == "buy" and current < 0) or (target_dir == "sell" and current > 0):
            close_vol = abs(current)
            close_dir = "buy" if current < 0 else "sell"  # 平空买入, 平多卖出
            # 穿盘口 + 3 tick, 立即成交
            price = self._aggressive_close_price(kline.close, close_dir, ticks=3)
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=close_vol, price=price, order_direction=close_dir,
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, close_vol, price,
                                 urgency="critical", direction=close_dir, kind="close")
                self.output(
                    f"[BAR {self._bar_idx}] 反向平仓 {close_dir} {close_vol}手 @ {price:.1f}"
                )
                self.state_map.last_action = f"反向平 {close_vol}"
                feishu("close", p.instrument_id,
                       f"**反向平仓** {close_dir} {close_vol}手 @ {price:,.1f}\n"
                       f"准备下一步开 {target_dir} {p.max_lots}手")
            # 同 bar 内也启动 executor 开新方向(effective_pos 视作 0)
            self._start_entry_executor(target_dir, p.max_lots, effective_pos=0, kline=kline)
            self._save()
            return

        # Case 3: 同方向但未到目标, 或者从 0 开仓
        delta = target_signed - current
        open_vol = abs(delta)
        self._start_entry_executor(target_dir, open_vol, effective_pos=current, kline=kline)
        self._save()

    def _start_entry_executor(self, direction: str, vol: int, effective_pos: int, kline) -> None:
        """统一的 executor 启动入口."""
        if self._entry is None:
            return
        p = self.params_map
        # 保证金预检
        acct = self._get_account()
        if acct and kline.close * self._multiplier * vol * 0.15 > acct.available * 0.6:
            self.output(f"[BAR {self._bar_idx}] 保证金不足, 跳过 OPEN {direction}")
            feishu("error", p.instrument_id,
                   f"**保证金不足** {direction} {vol}手")
            return

        bar_total = _freq_to_sec(p.kline_style)
        actions = self._entry.on_signal(
            target=vol, direction=direction,
            now=datetime.now(), current_position=effective_pos,
            forecast=10.0,
            bar_total_sec=bar_total,
        )
        for a in actions:
            self._apply_entry_action(a)
        self.output(
            f"[BAR {self._bar_idx}] executor 启动 {direction} {vol}手 (effective_pos={effective_pos})"
        )
        feishu("open", p.instrument_id,
               f"**ENTRY 启动** {direction} {vol}手\n"
               f"Bar {self._bar_idx} | 当前持仓={effective_pos} | 目标={vol}")
        self.state_map.last_action = f"OPEN {direction} {vol}"

    def _aggressive_close_price(self, last_price: float, direction: str, ticks: int = 3) -> float:
        """平仓穿盘口 N tick, 优先成交."""
        if self._pricer is None:
            return last_price
        u = ticks / 10.0  # 映射到 urgency
        return self._pricer.price_with_urgency_score(direction, u)

    # ══════════════════════════════════════════════════════════════════════
    #  成交
    # ══════════════════════════════════════════════════════════════════════

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_id.discard(trade.order_id)
        self._om.on_fill(trade.order_id)

        direction = "buy" if "买" in str(trade.direction) else "sell"
        slip = self._slip.on_fill(trade.price, trade.volume, direction)
        if slip != 0:
            self.output(f"[滑点] {slip:.1f}ticks")

        claimed_by_entry = False
        if self._entry is not None:
            claimed_by_entry = self._entry.on_trade(
                trade.order_id, trade.price, trade.volume, datetime.now()
            )
        if not claimed_by_entry:
            self._unknown_oid_count += 1
            if self._unknown_oid_count <= 5 or self._unknown_oid_count % 20 == 0:
                self.output(
                    f"[ON_TRADE] 未归属 oid={trade.order_id} vol={trade.volume} "
                    f"count={self._unknown_oid_count}"
                )

        # 更新本地 avg_price / peak / trough
        pos = self.get_position(self.params_map.instrument_id)
        actual = pos.net_position if pos else 0

        if actual > 0 and direction == "buy":
            # 多头加仓/建仓
            old_pos = max(0, actual - trade.volume)
            if old_pos > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old_pos + trade.price * trade.volume) / actual
            else:
                self.avg_price = trade.price
            if trade.price > self.peak_price or self.peak_price == 0:
                self.peak_price = trade.price
        elif actual < 0 and direction == "sell":
            # 空头加仓/建仓
            old_pos = min(0, actual + trade.volume)
            old_abs = abs(old_pos)
            if old_abs > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old_abs + trade.price * trade.volume) / abs(actual)
            else:
                self.avg_price = trade.price
            if trade.price < self.trough_price or self.trough_price == 0:
                self.trough_price = trade.price
        elif actual == 0:
            # 完全平仓
            self.avg_price = 0.0
            self.peak_price = 0.0
            self.trough_price = 0.0

        self.state_map.net_pos = actual
        self._save()

    # ══════════════════════════════════════════════════════════════════════
    #  状态持久化
    # ══════════════════════════════════════════════════════════════════════

    def _save(self):
        state = {
            "avg_price": self.avg_price,
            "peak_price": self.peak_price,
            "trough_price": self.trough_price,
            "bar_idx": self._bar_idx,
            "trading_day": self._current_td,
        }
        if self._risk is not None:
            state.update(self._risk.get_state())
        save_state(state, name=STRATEGY_NAME)

    # ══════════════════════════════════════════════════════════════════════
    #  诊断
    # ══════════════════════════════════════════════════════════════════════

    @property
    def main_indicator_data(self):
        return {
            "target": self.state_map.target,
            "net_pos": self.state_map.net_pos,
        }
