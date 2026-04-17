"""
================================================================================
  TestScaledEntry — 专门测试 ScaledEntryExecutor 的三模式验证策略 (v2)
================================================================================

  模拟盘三种测试模式(通过 `test_mode` 参数切换):

  ── Mode A: "hold_long" ──────────────────────────────────────────────────────
    目的: 观察 BOTTOM → OPP → FORCE → COMPLETE 完整入场周期
    逻辑: Bar 1 开多 target_lots (默认 20 — 比 10 大, 更可能触发 FORCE)
          Bar 2+: 不开新仓, 持仓等执行完成 (若成交了就持有)
    观察点:
      - [ENTRY] 日志 urgency 从低 (BOTTOM, ~0.1-0.3) 到高 (FORCE, ~0.7-1.0)
      - 飞书 "ENTRY BOTTOM / OPP 首笔 / FORCE / BOTTOM OVERDUE / COMPLETE"
      - state_map.entry_progress 从 bottom → opp → force → idle

  ── Mode B: "reversal" ───────────────────────────────────────────────────────
    目的: 测试方向翻转 — close 和 open 严格分 bar, 不让 oid 同 bar 竞争
    4-bar 周期: Bar 1 开多 → Bar 2 清空 → Bar 3 开空 → Bar 4 清空
    观察点:
      - 多头 / 空头两个方向的 executor 对称性
      - CLOSE 单与 executor 入场单独立, _unknown_oid_count 递增 (每次 CLOSE +1)
      - 开多与开空的 BOTTOM 反应对称

  ── Mode C: "stop_test" ──────────────────────────────────────────────────────
    目的: 测试止损触发 + LOCKED + 下 bar 解锁
    逻辑: 使用窄止损 (默认 hard=0.3%, trail=0.2%)
          每 bar 若 net_pos==0: 开 long target_lots
          价格波动大概率触发止损 → executor.on_stop_triggered() → LOCKED
          下 bar 新信号: LOCKED 自动解锁, 重新进 BOTTOM
    观察点:
      - [HARD_STOP][TICK] 或 [TRAIL_STOP][M1] 日志
      - 飞书 "硬止损 (tick触发)" / "移动止损 (tick触发)"
      - executor state LOCKED → 下 bar 变 BOTTOM
      - 每 bar 多次循环验证稳定性

  ── 所有模式通用 ────────────────────────────────────────────────────────────
    - pricer 首 tick dump [AggressivePricer][schema] 字段
    - RollingVWAP 跨 bar 滑窗
    - Tick 级硬止损 + M1 级移动止损 (stop_test 模式窄止损)
    - _unknown_oid_count 观察
    - 保证金预检
    - session_guard 非交易时段禁发

  部署: Windows pyStrategy/self_strategy/TestScaledEntry.py
         建议 3 个合约并行跑 3 个 mode (如 al2607/ag2606/cu2607)
================================================================================
"""
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

VALID_MODES = ("hold_long", "reversal", "stop_test")


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
    # 合约 & 基础
    exchange: str = Field(default="SHFE", title="交易所")
    instrument_id: str = Field(default="al2607", title="合约")
    kline_style: str = Field(default="H1", title="K线周期")
    capital: float = Field(default=1_000_000, title="名义本金")

    # 测试模式 (hold_long / reversal / stop_test)
    test_mode: str = Field(default="hold_long", title="测试模式")

    # 目标仓位
    target_lots: int = Field(default=20, title="hold_long/stop_test 目标手数")
    reversal_max_lots: int = Field(default=10, title="reversal 单方向手数")
    bottom_lots: int = Field(default=2, title="底仓手数")

    # 止损 (stop_test 模式默认窄, 其他模式宽松)
    hard_stop_pct: float = Field(default=1.0, title="硬止损(%)")
    trailing_pct: float = Field(default=0.6, title="移动止损(%)")
    stop_test_hard_pct: float = Field(default=0.3, title="stop_test 硬止损(%)")
    stop_test_trail_pct: float = Field(default=0.2, title="stop_test 移动止损(%)")

    # 运维
    flatten_minutes: int = Field(default=5, title="距收盘提前清仓分钟")
    sim_24h: bool = Field(default=True, title="24h仿真模式")


class State(BaseState):
    bar_idx: int = Field(default=0, title="已处理bar数")
    mode: str = Field(default="---", title="当前模式")
    phase: str = Field(default="---", title="本 bar 阶段")
    target: int = Field(default=0, title="本 bar 目标(带符号)")
    net_pos: int = Field(default=0, title="当前持仓")
    avg_price: float = Field(default=0.0, title="均价")
    entry_progress: str = Field(default="---", title="入场进度")
    last_action: str = Field(default="---", title="上次操作")
    last_stop: str = Field(default="---", title="上次止损")
    trading_day: str = Field(default="", title="交易日")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════


class TestScaledEntry(BaseStrategy):
    """ScaledEntryExecutor 三模式验证策略"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        # 持仓状态
        self.avg_price = 0.0
        self.peak_price = 0.0
        self.trough_price = 0.0
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

        # 元信息
        self._investor_id = ""
        self._multiplier = 5
        self._current_td = ""
        self._bar_idx = 0
        self._unknown_oid_count = 0

        # hold_long 模式:记录首次开仓 bar, 后续不再开
        self._hold_long_opened = False

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    def _eff_hard_pct(self) -> float:
        p = self.params_map
        return p.stop_test_hard_pct if p.test_mode == "stop_test" else p.hard_stop_pct

    def _eff_trail_pct(self) -> float:
        p = self.params_map
        return p.stop_test_trail_pct if p.test_mode == "stop_test" else p.trailing_pct

    # ══════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════

    def on_start(self):
        p = self.params_map
        if p.test_mode not in VALID_MODES:
            self.output(f"[启动错误] 无效 test_mode={p.test_mode}, 退回 hold_long")
            p.test_mode = "hold_long"

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
            self._hold_long_opened = saved.get("hold_long_opened", False)

        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance)

        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        self.state_map.mode = p.test_mode

        mode_note = {
            "hold_long": f"开多 {p.target_lots}手 + 持有不翻转",
            "reversal": f"4-bar 周期 ±{p.reversal_max_lots}手 方向翻转",
            "stop_test": f"开多 {p.target_lots}手 + 窄止损 hard={p.stop_test_hard_pct}% trail={p.stop_test_trail_pct}%",
        }.get(p.test_mode, "unknown")

        self.output(f"[启动] {STRATEGY_NAME} mode={p.test_mode} 持仓={actual}")
        feishu("start", p.instrument_id,
               f"**测试策略启动** {STRATEGY_NAME}\n"
               f"模式: {p.test_mode}\n"
               f"逻辑: {mode_note}\n"
               f"合约: {p.instrument_id} | 乘数: {self._multiplier}\n"
               f"持仓: {actual}手 | bar_idx: {self._bar_idx}")
        super().on_start()

    def on_stop(self):
        self._save()
        feishu("shutdown", self.params_map.instrument_id,
               f"**测试策略停止** mode={self.params_map.test_mode}\n"
               f"持仓: {self.state_map.net_pos}手 | bar_idx: {self._bar_idx}\n"
               f"unknown_oid_count={self._unknown_oid_count}")
        super().on_stop()

    @property
    def main_indicator_data(self):
        return {"target": self.state_map.target, "net_pos": self.state_map.net_pos}

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
            self._on_tick_stops(tick)
        except Exception as e:
            self.output(f"[stops异常] {type(e).__name__}: {e}")

        try:
            self._drive_entry(tick)
        except Exception as e:
            self.output(f"[entry异常] {type(e).__name__}: {e}")

        try:
            self._on_tick_aux(tick)
        except Exception as e:
            self.output(f"[aux异常] {type(e).__name__}: {e}")

    def _on_tick_stops(self, tick: TickData):
        """Tick 级止损 — 所有模式通用, stop_test 模式用窄阈值."""
        if not self.trading:
            return
        if self._guard is not None and not self._guard.should_trade():
            return

        p = self.params_map
        pos = self.get_position(p.instrument_id)
        if pos is None:
            return
        net_pos = pos.net_position
        price = tick.last_price

        self._risk.update_peak_trough_tick(price, net_pos)
        if net_pos > 0:
            self.peak_price = self._risk.peak_price
        elif net_pos < 0:
            self.trough_price = self._risk.trough_price

        if net_pos == 0:
            return

        # Tick 级硬止损
        action, reason = self._risk.check_hard_stop_tick(
            price=price, avg_price=self.avg_price,
            net_pos=net_pos, hard_stop_pct=self._eff_hard_pct(),
        )
        if action:
            self.output(f"[{action}][TICK] {reason}")
            self._exec_stop(price, action, reason)
            return

        # M1 级移动止损
        action, reason = self._risk.check_trail_minutely(
            price=price, now=datetime.now(),
            net_pos=net_pos, trailing_pct=self._eff_trail_pct(),
        )
        if action:
            self.output(f"[{action}][M1] {reason}")
            self._exec_stop(price, action, reason)

    def _exec_stop(self, price: float, action: str, reason: str) -> None:
        """止损统一执行路径:多空对称."""
        p = self.params_map
        if self._guard is not None and not self._guard.should_trade():
            return
        pos = self.get_position(p.instrument_id)
        if pos is None:
            return
        actual = pos.net_position
        if actual == 0:
            return

        vol = abs(actual)
        close_dir = "sell" if actual > 0 else "buy"

        # 撤所有挂单 + 同步 executor
        for oid in list(self.order_id):
            self.cancel_order(oid)
            if self._entry is not None:
                self._entry.register_cancelled(oid)

        # 止损穿盘口 urgency=0.8 (约 8 tick)
        close_price = self._pricer.price_with_urgency_score(close_dir, 0.8) if self._pricer else price

        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=close_price, order_direction=close_dir,
        )
        if oid is None:
            self.output(f"[STOP] auto_close_position 返回 None, 保留信号下 bar 重试")
            feishu("error", p.instrument_id,
                   f"**止损发单失败** {action} {close_dir} {vol}手")
            return

        self.order_id.add(oid)
        self._om.on_send(oid, vol, close_price,
                         urgency="critical", direction=close_dir, kind="close")

        # 计算 PnL
        if actual > 0:
            pnl_pct = (close_price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0.0
        else:
            pnl_pct = (self.avg_price - close_price) / self.avg_price * 100 if self.avg_price > 0 else 0.0
        abs_pnl = self._perf.on_close(self.avg_price, close_price, vol,
                                       direction="long" if actual > 0 else "short")

        labels = {"HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损"}
        label = labels.get(action, action)
        self.state_map.last_stop = f"{label} pnl={pnl_pct:+.2f}%"
        feishu(action.lower(), p.instrument_id,
               f"**{label}** (tick触发) {vol}手 @ {close_price:,.1f}\n"
               f"方向: {'多' if actual>0 else '空'} | 逻辑: {reason}\n"
               f"盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})\n"
               f"持仓: {actual} -> 0")

        # 本地状态清理(on_trade 也会清, 此处防御性重置)
        self.avg_price = 0.0
        self.peak_price = 0.0
        self.trough_price = 0.0

        # 通知 executor → LOCKED
        if self._entry is not None:
            stop_actions = self._entry.on_stop_triggered(datetime.now())
            for a in stop_actions:
                self._apply_entry_action(a)

        # 清 risk 内部极值
        self._risk.peak_price = 0.0
        self._risk.trough_price = 0.0
        self._risk._last_trail_minute = None
        self._save()

    def _drive_entry(self, tick: TickData) -> None:
        if self._entry is None or self._rvwap is None or self._pricer is None:
            return
        if not self.trading:
            return
        if self._guard is not None and not self._guard.should_trade():
            return

        p = self.params_map
        pos = self.get_position(p.instrument_id)
        net_pos = pos.net_position if pos else 0

        # forecast 不硬编码为 10: 用一个中等 5.0 让 urgency 主要受时间 + 缺口驱动
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
                self._om.on_send(oid, a.vol, a.price, urgency="entry",
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
    #  Bar 回调 — 三模式主逻辑
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
        self.state_map.bar_idx = self._bar_idx

        pos = self.get_position(p.instrument_id)
        net_pos = pos.net_position if pos else 0
        self.state_map.net_pos = net_pos

        self.output(
            f"[BAR {self._bar_idx}] mode={p.test_mode} 当前持仓={net_pos} close={kline.close:.1f} "
            f"entry_state={self._entry.state.value if self._entry else '-'}"
        )

        # 分发到各 mode
        if p.test_mode == "hold_long":
            self._run_hold_long(kline, net_pos)
        elif p.test_mode == "reversal":
            self._run_reversal(kline, net_pos)
        elif p.test_mode == "stop_test":
            self._run_stop_test(kline, net_pos)
        else:
            self.output(f"[未知模式] {p.test_mode}")

        self._save()

    # ────────────────────────────────────────────────────────────────────
    # Mode A: hold_long — 开一次, 然后持有
    # ────────────────────────────────────────────────────────────────────
    def _run_hold_long(self, kline: KLineData, net_pos: int) -> None:
        p = self.params_map
        self.state_map.phase = "bar1-open" if not self._hold_long_opened else f"holding (pos={net_pos})"

        if self._hold_long_opened:
            self.output(f"[HOLD_LONG] 已建仓, 继续持有 net_pos={net_pos}")
            return

        # Bar 1: 开多 target_lots
        self._hold_long_opened = True
        target = p.target_lots
        self.state_map.target = target
        self._start_executor("buy", target, effective_pos=net_pos, kline=kline,
                              reason="hold_long bar1 open")

    # ────────────────────────────────────────────────────────────────────
    # Mode B: reversal — 4-bar 周期 open long / close / open short / close
    # ────────────────────────────────────────────────────────────────────
    def _run_reversal(self, kline: KLineData, net_pos: int) -> None:
        p = self.params_map
        phase = self._bar_idx % 4    # 1, 2, 3, 0

        if phase == 1:
            # 开多
            self.state_map.phase = "reversal-open-long"
            self.state_map.target = p.reversal_max_lots
            self._start_executor("buy", p.reversal_max_lots,
                                 effective_pos=net_pos, kline=kline,
                                 reason="reversal phase1 open long")
        elif phase == 2:
            # 清多
            self.state_map.phase = "reversal-close-long"
            self.state_map.target = 0
            self._close_all_direct(kline, net_pos, reason="reversal phase2 close long")
        elif phase == 3:
            # 开空
            self.state_map.phase = "reversal-open-short"
            self.state_map.target = -p.reversal_max_lots
            self._start_executor("sell", p.reversal_max_lots,
                                 effective_pos=net_pos, kline=kline,
                                 reason="reversal phase3 open short")
        else:  # phase == 0 (bar 4)
            # 清空
            self.state_map.phase = "reversal-close-short"
            self.state_map.target = 0
            self._close_all_direct(kline, net_pos, reason="reversal phase0 close short")

    # ────────────────────────────────────────────────────────────────────
    # Mode C: stop_test — 每 bar 若空仓则开多, 窄止损
    # ────────────────────────────────────────────────────────────────────
    def _run_stop_test(self, kline: KLineData, net_pos: int) -> None:
        p = self.params_map

        if net_pos != 0:
            self.state_map.phase = f"stop_test-holding pos={net_pos}"
            self.output(f"[STOP_TEST] 已持仓 net_pos={net_pos}, 等止损或 executor 完成")
            return

        # net_pos == 0: 开新 long (executor 会自动从 LOCKED 解锁)
        self.state_map.phase = "stop_test-open-long"
        self.state_map.target = p.target_lots
        self._start_executor("buy", p.target_lots,
                             effective_pos=0, kline=kline,
                             reason=f"stop_test bar{self._bar_idx} open long")

    # ────────────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────────────

    def _start_executor(self, direction: str, vol: int, effective_pos: int,
                        kline: KLineData, reason: str) -> None:
        """统一启动 executor, 含保证金预检."""
        if self._entry is None:
            return
        p = self.params_map

        # 保证金预检
        acct = self._get_account()
        if acct and kline.close * self._multiplier * vol * 0.15 > acct.available * 0.6:
            self.output(f"[EXECUTOR] 保证金不足, 跳过 {direction} {vol}手")
            feishu("error", p.instrument_id,
                   f"**保证金不足** {direction} {vol}手 (mode={p.test_mode})")
            return

        bar_total = _freq_to_sec(p.kline_style)
        actions = self._entry.on_signal(
            target=vol, direction=direction,
            now=datetime.now(), current_position=effective_pos,
            forecast=5.0, bar_total_sec=bar_total,
        )
        for a in actions:
            self._apply_entry_action(a)

        self.output(
            f"[EXECUTOR] 启动 {direction} {vol}手 "
            f"(effective_pos={effective_pos}, reason={reason})"
        )
        self.state_map.last_action = f"OPEN {direction} {vol}"
        feishu("open", p.instrument_id,
               f"**EXECUTOR 启动** {direction} {vol}手\n"
               f"Bar {self._bar_idx} | effective_pos={effective_pos}\n"
               f"原因: {reason}")

    def _close_all_direct(self, kline: KLineData, net_pos: int, reason: str) -> None:
        """直接 auto_close_position 平仓 (不走 executor)."""
        p = self.params_map
        if net_pos == 0:
            self.output(f"[CLOSE] 无持仓, 跳过")
            return

        vol = abs(net_pos)
        close_dir = "sell" if net_pos > 0 else "buy"
        close_price = self._pricer.price_with_urgency_score(close_dir, 0.5) if self._pricer else kline.close

        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=close_price, order_direction=close_dir,
        )
        if oid is None:
            self.output(f"[CLOSE] auto_close_position 返回 None")
            return

        self.order_id.add(oid)
        self._om.on_send(oid, vol, close_price,
                         urgency="cross", direction=close_dir, kind="close")

        if net_pos > 0:
            pnl_pct = (close_price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0.0
        else:
            pnl_pct = (self.avg_price - close_price) / self.avg_price * 100 if self.avg_price > 0 else 0.0

        self.output(
            f"[CLOSE] {close_dir} {vol}手 @ {close_price:.1f} pnl={pnl_pct:+.2f}% "
            f"({reason})"
        )
        self.state_map.last_action = f"CLOSE {close_dir} {vol}"
        feishu("close", p.instrument_id,
               f"**主动平仓** {close_dir} {vol}手 @ {close_price:,.1f}\n"
               f"盈亏: {pnl_pct:+.2f}% | 原因: {reason}")

    # ══════════════════════════════════════════════════════════════════════
    #  成交回调
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
                    f"(可能是 CLOSE/STOP 路径), count={self._unknown_oid_count}"
                )

        # 更新 avg_price / peak / trough
        pos = self.get_position(self.params_map.instrument_id)
        actual = pos.net_position if pos else 0

        if actual > 0 and direction == "buy":
            old_pos = max(0, actual - trade.volume)
            if old_pos > 0 and self.avg_price > 0:
                self.avg_price = (
                    (self.avg_price * old_pos + trade.price * trade.volume) / actual
                )
            else:
                self.avg_price = trade.price
            if trade.price > self.peak_price or self.peak_price == 0:
                self.peak_price = trade.price
        elif actual < 0 and direction == "sell":
            old_abs = abs(min(0, actual + trade.volume))
            actual_abs = abs(actual)
            if old_abs > 0 and self.avg_price > 0:
                self.avg_price = (
                    (self.avg_price * old_abs + trade.price * trade.volume) / actual_abs
                )
            else:
                self.avg_price = trade.price
            if trade.price < self.trough_price or self.trough_price == 0:
                self.trough_price = trade.price
        elif actual == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0
            self.trough_price = 0.0

        self.state_map.net_pos = actual
        self.state_map.avg_price = round(self.avg_price, 2)
        self._save()

    # ══════════════════════════════════════════════════════════════════════
    #  持久化
    # ══════════════════════════════════════════════════════════════════════

    def _save(self):
        state = {
            "avg_price": self.avg_price,
            "peak_price": self.peak_price,
            "trough_price": self.trough_price,
            "bar_idx": self._bar_idx,
            "trading_day": self._current_td,
            "hold_long_opened": self._hold_long_opened,
        }
        if self._risk is not None:
            state.update(self._risk.get_state())
        save_state(state, name=STRATEGY_NAME)
