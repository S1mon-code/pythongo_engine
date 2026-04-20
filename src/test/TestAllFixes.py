"""
================================================================================
  TestAllFixes — 2026-04-20 全部修复的实盘 smoke test
================================================================================

  不是真实交易策略,**专门 smoke test** fleet-wide 修复在实盘跑起来没问题.
  每 2 bar 强制切换仓位 → 极高频成交 → 每个修复路径都会被反复走到.

  ── 8 项修复 × 覆盖路径 ──────────────────────────────────────────────
  1. Bug A: order.cancel_volume (代替 order.volume)
     → on_order_cancel 触发时打印 [BUG_A_VERIFY] cancel_volume=N
     → 每 N bar (默认 5) 主动发假 oid cancel 模拟额外路径

  2. Bug B: trade.direction 健壮识别 + DIAG
     → on_trade 打印 [BUG_B_DIAG] direction={!r} offset={!r}
     → 累计到 state_map.last_direction 给 UI 看
     → 健壮 pattern cover "0"/"1"/"buy"/"sell"/"买"/"卖"

  3. market=True → market=False
     → 所有 send_order / auto_close_position 不传 market
     → 依赖 _aggressive_price 穿盘口挂限价

  4. on_error 0004 流控 (throttle_on_error)
     → 真实 0004 由 broker 触发(无法本地强制)
     → 若发生 → trading=False → Timer(2s) → trading=True
     → 观察: 飞书 error 后 2 秒内 trading 能恢复就算通过

  5. Executor crash recovery
     → on_start 检测 saved["executor"]["pending_orders"]
     → force_lock + oid 加回 order_id + 飞书 WARNING
     → 测试方法: 持仓中点暂停 → 等 30 秒 → 再点运行

  6. Startup eval
     → 首 tick 就绪时 _evaluate_startup
     → 简化版只验证路径不崩 + re-entry guard 能拦截

  7. last_exit_bar_ts re-entry guard
     → 平仓/止损时写入 _current_bar_ts
     → 同 bar 启动评估会被拦截 (飞书 "同 bar 重入场被拦截")

  8. Risk on_day_change position_profit (过夜浮盈剔除)
     → on_start + 日切时 调 on_day_change(balance, position_profit)
     → 打印 [DAY_CHANGE_TEST] before/after start_eq

  ── 信号设计 (保证高频触发) ──────────────────────────────────────────
  每 bar:
  - 奇数 bar + net_pos==0 → 开多 target_lots 手 (走 executor.on_signal)
  - 偶数 bar + net_pos>0  → 平仓 (走 auto_close_position 限价单)
  - 每 cancel_every_n (5) bar → 额外发一个假 oid cancel (Bug A 路径)

  窄止损 (hard=0.3%, trail=0.2%) → 实盘价波动大概率触发 LOCKED

  ── 推荐参数 ──────────────────────────────────────────────────────
  合约: DCE i2505 (铁矿,流动性好,tick_size=0.5)
  周期: M1 (每分钟一根,快速迭代)
  target_lots: 2 (小手数安全)
  max_lots: 5
  capital: 1_000_000

  ⚠️ 上线前清空 state/ 目录一次!
================================================================================
"""
import time
from datetime import datetime

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.contract_info import get_multiplier, get_tick_size
from modules.error_handler import throttle_on_error
from modules.execution import EntryParams, ExecAction, ScaledEntryExecutor
from modules.feishu import feishu
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.persistence import load_state, save_state
from modules.position_sizing import apply_buffer, calc_optimal_lots
from modules.pricing import AggressivePricer
from modules.risk import RiskManager, check_stops
from modules.rolling_vwap import RollingVWAP
from modules.rollover import check_rollover
from modules.session_guard import SessionGuard
from modules.slippage import SlippageTracker
from modules.trading_day import DAY_START_HOUR, get_trading_day, is_new_day


STRATEGY_NAME = "TestAllFixes"


def _freq_to_sec(kline_style) -> int:
    """kline_style → 秒数 (bar 周期)."""
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


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG / PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════


class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2505", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")
    target_lots: int = Field(default=2, title="每次开仓手数")
    max_lots: int = Field(default=5, title="最大持仓")
    capital: float = Field(default=1_000_000, title="配置资金")
    hard_stop_pct: float = Field(default=0.3, title="硬止损(%) — 窄")
    trailing_pct: float = Field(default=0.2, title="移动止损(%) — 窄")
    equity_stop_pct: float = Field(default=2.0, title="权益止损(%)")
    flatten_minutes: int = Field(default=5, title="即将收盘(分钟)")
    sim_24h: bool = Field(default=True, title="24H模拟盘模式")
    cancel_every_n: int = Field(default=5, title="每N根bar主动撤假oid")


class State(BaseState):
    net_pos: int = Field(default=0, title="净持仓")
    bar_count: int = Field(default=0, title="Bar计数")
    avg_price: float = Field(default=0.0, title="均价")
    peak_price: float = Field(default=0.0, title="峰价")
    equity: float = Field(default=0.0, title="权益")
    drawdown: str = Field(default="---", title="回撤")
    last_action: str = Field(default="---", title="上次操作")
    last_direction: str = Field(default="---", title="上次direction值(DIAG)")
    diag_count: int = Field(default=0, title="DIAG触发次数")
    entry_progress: str = Field(default="---", title="入场进度")
    session: str = Field(default="---", title="交易时段")
    pending: str = Field(default="---", title="待执行")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════


class TestAllFixes(BaseStrategy):
    """Smoke test 验证 2026-04-20 所有 fleet-wide 修复."""

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
        self._bar_count = 0

        # 账户 / 风控
        self._investor_id = ""
        self._risk: RiskManager | None = None
        self._current_td = ""
        self._daily_review_sent = False

        # 模块
        self._guard: SessionGuard | None = None
        self._slip: SlippageTracker | None = None
        self._hb: HeartbeatMonitor | None = None
        self._om = OrderMonitor()
        self._perf: PerformanceTracker | None = None
        self._pricer: AggressivePricer | None = None
        self._multiplier = 1

        self._rvwap: RollingVWAP | None = None
        self._entry: ScaledEntryExecutor | None = None

        # Startup eval (2026-04-20 修复 #6)
        self._needs_startup_eval = True
        # Re-entry guard (2026-04-20 修复 #7)
        self._last_exit_bar_ts: int = 0

        # DIAG 统计
        self._diag_direction_samples: list[str] = []
        self._unknown_oid_count = 0
        self._fake_cancel_count = 0

    @property
    def main_indicator_data(self):
        return {"bar": float(self._bar_count % 100)}

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

        # Module init
        self._multiplier = get_multiplier(p.instrument_id)
        self._pricer = AggressivePricer(tick_size=get_tick_size(p.instrument_id))
        self._rvwap = RollingVWAP(window_seconds=1800)
        self._entry = ScaledEntryExecutor(EntryParams(bottom_lots=1))

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

        # ─────────────────────────────────────────────────────────────
        # 修复 #5 验证: Executor crash recovery
        # ─────────────────────────────────────────────────────────────
        saved = load_state(STRATEGY_NAME)
        if saved:
            self._risk.load_state(saved)
            self.peak_price = saved.get("peak_price", 0.0)
            self.avg_price = saved.get("avg_price", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._last_exit_bar_ts = saved.get("last_exit_bar_ts", 0)
            self._bar_count = saved.get("bar_count", 0)

            exec_state = saved.get("executor") or {}
            if exec_state:
                self._entry.load_state(exec_state)
                pending_orders = exec_state.get("pending_orders", [])
                if pending_orders:
                    self._entry.force_lock()
                    for item in pending_orders:
                        oid = item.get("oid")
                        if oid is not None:
                            self.order_id.add(oid)
                    pending_total = sum(i.get("vol", 0) for i in pending_orders)
                    self.output(
                        f"[CRASH_RECOVERY_TEST] executor 遗留 {len(pending_orders)} 单 "
                        f"({pending_total}手), force_lock, oid 加回 cancel 队列"
                    )
                    feishu("warning", p.instrument_id,
                           f"**[TEST] 崩溃恢复** executor 遗留 "
                           f"{len(pending_orders)}单 ({pending_total}手)\n"
                           f"state={exec_state.get('state')} "
                           f"filled={exec_state.get('filled')}/{exec_state.get('target')}\n"
                           f"→ 已 LOCKED 本 bar, cancel sweep 将清理")

            self.output(
                f"[恢复] bar_count={self._bar_count} "
                f"last_exit_bar={self._last_exit_bar_ts} "
                f"avg={self.avg_price:.1f} peak_eq={self._risk.peak_equity:.0f}"
            )

        # ─────────────────────────────────────────────────────────────
        # 修复 #8 验证: on_day_change 剔除过夜浮盈
        # ─────────────────────────────────────────────────────────────
        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance, acct.position_profit)
                self.output(
                    f"[DAY_CHANGE_TEST] 首次 on_day_change: "
                    f"balance={acct.balance:.0f} - position_profit={acct.position_profit:.0f} "
                    f"→ start_eq={self._risk.daily_start_eq:.0f}"
                )

        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.session = self._guard.get_status()

        super().on_start()
        self.output(
            f"=== TestAllFixes 启动 ===\n"
            f"合约 {p.instrument_id} {p.kline_style} | 乘数 {self._multiplier} | "
            f"target_lots={p.target_lots} max_lots={p.max_lots} | "
            f"窄止损 hard={p.hard_stop_pct}% trail={p.trailing_pct}% | "
            f"sim_24h={p.sim_24h}"
        )
        feishu("start", p.instrument_id,
               f"**[TEST] TestAllFixes 启动**\n"
               f"合约 {p.instrument_id} {p.kline_style}\n"
               f"target_lots {p.target_lots} max_lots {p.max_lots}\n"
               f"窄止损 hard={p.hard_stop_pct}% trail={p.trailing_pct}%\n"
               f"信号: 每 2 bar 开/平仓循环\n"
               f"测试: BugA/BugB/market=False/throttle/crash_rec/startup_eval/"
               f"re_entry_guard/overnight")

    def on_stop(self):
        self._save()
        samples = set(self._diag_direction_samples)
        feishu("shutdown", self.params_map.instrument_id,
               f"**[TEST] 停止**\n"
               f"持仓: {self.state_map.net_pos}手\n"
               f"bar_count: {self._bar_count}\n"
               f"假 oid cancel 次数: {self._fake_cancel_count}\n"
               f"DIAG 采样: {len(samples)} 种 direction 值 → {samples}\n"
               f"未知 oid 次数: {self._unknown_oid_count}\n"
               f"{self._slip.format_report() if self._slip else ''}")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick 路径
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

        # 修复 #6 验证: Startup eval (只跑一次)
        if (self._needs_startup_eval
                and self.trading
                and self._pricer is not None and self._pricer.last > 0
                and self._guard is not None and self._guard.should_trade()):
            try:
                self._evaluate_startup()
            except Exception as e:
                self.output(f"[启动评估异常] {type(e).__name__}: {e}")
                feishu("error", self.params_map.instrument_id,
                       f"**[TEST] startup eval 异常** {type(e).__name__}: {e}")
            finally:
                self._needs_startup_eval = False

        # Tick 级止损 (窄止损 → 大概率触发 → 测 last_exit_bar_ts)
        try:
            self._on_tick_stops(tick)
        except Exception as e:
            self.output(f"[stops异常] {type(e).__name__}: {e}")

        # Executor 驱动 (market=False 限价单)
        try:
            self._drive_entry(tick)
        except Exception as e:
            self.output(f"[entry异常] {type(e).__name__}: {e}")

    def _evaluate_startup(self) -> None:
        """修复 #6 + #7 验证 + 扩展 4 分支测试 (2026-04-20 v2).

        覆盖 Simon 每日 18:00 清算 + 21:00 重开的真实场景:
        - 若 re-entry guard 拦截 → 跳过
        - 若当前持仓>0 → **强制平仓** (测 AL V8 target=0 分支)
        - 若当前持仓=0 → 等 bar callback 开仓 (正常路径)
        """
        p = self.params_map
        cur_bar_ts = self._current_bar_ts()

        # Re-entry guard 测试
        if self._last_exit_bar_ts >= cur_bar_ts:
            self.output(
                f"[STARTUP_EVAL] re-entry guard 拦截: "
                f"bar_ts={cur_bar_ts} ≤ last_exit={self._last_exit_bar_ts}"
            )
            feishu("warning", p.instrument_id,
                   f"**[TEST] startup eval 被 re-entry guard 拦截**\n"
                   f"cur_bar_ts={cur_bar_ts} last_exit={self._last_exit_bar_ts}")
            return

        # 检查持仓 — 模拟 AL V8 的 4 分支决策
        pos = self.get_position(p.instrument_id)
        net_pos = pos.net_position if pos else 0

        if net_pos > 0:
            # 有隔夜持仓 → 强制平仓 (测试 target=0 → 平仓 分支)
            # 在真实策略里这对应"隔夜持仓 + 信号消失"的 target=0 case
            live = self._pricer.last if self._pricer else 0.0
            sell_price = self._aggressive_price(live, "sell", urgency="urgent")
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=net_pos, price=sell_price, order_direction="sell",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, net_pos, sell_price,
                                 urgency="urgent", direction="sell", kind="close")
            self._last_exit_bar_ts = self._current_bar_ts()
            self.output(
                f"[STARTUP_EVAL] 隔夜持仓 {net_pos}手 → 强制平仓 @ {sell_price:.1f} "
                f"(测 target=0 分支)"
            )
            feishu("close", p.instrument_id,
                   f"**[TEST] startup eval 平仓** {net_pos}手 @ {sell_price:,.1f}\n"
                   f"模拟隔夜持仓 + 信号消失场景\n"
                   f"bar_ts={cur_bar_ts} live={live:.1f}")
            self._save()
            return

        # 空仓 → 通过,等 bar callback 开仓
        self.output(
            f"[STARTUP_EVAL] 首 tick 通过 (net_pos=0), bar_ts={cur_bar_ts}, "
            f"等 bar callback 按奇偶开仓"
        )
        feishu("info", p.instrument_id,
               f"**[TEST] startup eval 通过**\n"
               f"bar_ts={cur_bar_ts} net_pos=0, 无持仓无需动作")

    def _on_tick_stops(self, tick: TickData):
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
        net_pos = pos.net_position
        price = tick.last_price

        self._risk.update_peak_trough_tick(price, net_pos)
        self.peak_price = self._risk.peak_price

        if net_pos <= 0:
            return

        # 硬止损
        action, reason = self._risk.check_hard_stop_tick(
            price=price, avg_price=self.avg_price,
            net_pos=net_pos, hard_stop_pct=p.hard_stop_pct,
        )
        if action:
            self.output(f"[{action}][TICK] {reason}")
            self._exec_stop_at_tick(price, action, reason)
            if self._entry is not None:
                for ea in self._entry.on_stop_triggered(datetime.now()):
                    self._apply_entry_action(ea)
            return

        # 移动止损 (M1 级)
        action, reason = self._risk.check_trail_minutely(
            price=price, now=datetime.now(),
            net_pos=net_pos, trailing_pct=p.trailing_pct,
        )
        if action:
            self.output(f"[{action}][M1] {reason}")
            self._exec_stop_at_tick(price, action, reason)
            if self._entry is not None:
                for ea in self._entry.on_stop_triggered(datetime.now()):
                    self._apply_entry_action(ea)

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

        actions = self._entry.on_tick(
            now=datetime.now(),
            last_price=tick.last_price,
            bid1=self._pricer.bid1,
            ask1=self._pricer.ask1,
            tick_size=self._pricer.tick_size,
            vwap_value=self._rvwap.value,
            forecast=5.0,  # 固定, 中等 urgency
            current_position=net_pos,
        )
        for a in actions:
            self._apply_entry_action(a)

        self.state_map.entry_progress = self._entry.progress_str()

    def _apply_entry_action(self, a: ExecAction) -> None:
        p = self.params_map
        if a.op == "submit":
            # 验证修复 #3: market=False (默认), 限价单
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

    # ══════════════════════════════════════════════════════════════════════
    #  K线回调 — 信号简单频繁
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

        if not self.trading:
            self._pending = None
            self._push_widget(kline)
            return

        if self._guard is not None and not self._guard.should_trade():
            self.state_map.session = self._guard.get_status()
            self._push_widget(kline)
            self.update_status_bar()
            return

        # 撤所有挂单 (同步 executor)
        for oid in list(self.order_id):
            self.cancel_order(oid)
            if self._entry is not None:
                self._entry.register_cancelled(oid)
        for oid in self._om.check_timeouts(self.cancel_order):
            if self._entry is not None:
                self._entry.register_cancelled(oid)

        self._bar_count += 1
        self.state_map.bar_count = self._bar_count

        pos = self.get_position(p.instrument_id)
        net_pos = pos.net_position if pos else 0
        self.state_map.net_pos = net_pos

        # 修复 #1 额外测试: 每 N bar 发一个假 oid cancel
        # (on_order_cancel 不会真触发 — 假 oid broker 不认 —
        # 但 cancel_order 本地返 -1, 不崩就证明 Bug A 路径 OK)
        if self._bar_count % p.cancel_every_n == 0:
            fake_oid = 900_000_000 + self._bar_count
            result = self.cancel_order(fake_oid)
            self._fake_cancel_count += 1
            self.output(
                f"[BUG_A_TEST] bar {self._bar_count}: "
                f"cancel fake_oid={fake_oid} → return {result} (-1=不存在)"
            )

        # 账户权益 + 每 10 bar 尝试 on_day_change re-test
        acct = self._get_account()
        if acct:
            self._risk.update(acct.balance)
            self.state_map.equity = round(acct.balance, 0)
            self.state_map.drawdown = f"{self._risk.drawdown_pct:.2%}"
            if self._bar_count % 10 == 0:
                # 修复 #8 复检 (非真实日切, 只为验证 API)
                self.output(
                    f"[DAY_CHANGE_RECHECK] bar {self._bar_count} "
                    f"balance={acct.balance:.0f} "
                    f"position_profit={acct.position_profit:.0f} "
                    f"current start_eq={self._risk.daily_start_eq:.0f}"
                )

        # 价格变量
        price = kline.close
        signal_price = 0.0

        # ──── 信号: 奇 bar 开多 / 偶 bar 平仓 ────
        if self._bar_count % 2 == 1 and net_pos == 0:
            # 开多
            target = min(p.target_lots, p.max_lots)
            if acct and price * self._multiplier * target * 0.15 > acct.available * 0.6:
                self.output(f"[保证金不足] bar {self._bar_count} 跳过开多")
                self._push_widget(kline)
                self.update_status_bar()
                return

            bar_total = _freq_to_sec(p.kline_style)
            actions = self._entry.on_signal(
                target=target, direction="buy",
                now=datetime.now(), current_position=net_pos,
                forecast=5.0, bar_total_sec=bar_total,
            )
            for ea in actions:
                self._apply_entry_action(ea)

            self._pending_reason = f"[TEST] bar {self._bar_count} 开多 {target}手"
            self.state_map.last_action = f"开多{target}手"
            self.output(f"[SIGNAL] bar {self._bar_count} 开多 target={target}")
            feishu("open", p.instrument_id,
                   f"**[TEST] 开多** target={target}手 @ {price:,.1f}\n"
                   f"bar {self._bar_count}")
            signal_price = price

        elif self._bar_count % 2 == 0 and net_pos > 0:
            # 平仓 (直接限价, 测 market=False)
            sell_price = self._aggressive_price(price, "sell", urgency="normal")
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=net_pos, price=sell_price, order_direction="sell",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, net_pos, sell_price,
                                 urgency="normal", direction="sell", kind="close")

            pnl_pct = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
            self.state_map.last_action = f"平{net_pos}手 {pnl_pct:+.2f}%"
            feishu("close", p.instrument_id,
                   f"**[TEST] 平仓** {net_pos}手 @ {price:,.1f}\n"
                   f"盈亏: {pnl_pct:+.2f}%\nbar {self._bar_count}")
            self.avg_price = 0.0
            self.peak_price = 0.0

            # 修复 #7 验证: 写入 last_exit_bar_ts
            self._last_exit_bar_ts = self._current_bar_ts()
            self.output(
                f"[RE_ENTRY_GUARD] bar {self._bar_count} 平仓, "
                f"设 last_exit_bar_ts={self._last_exit_bar_ts}"
            )
            self._save()
            signal_price = -price

        self.state_map.pending = self._pending or "---"
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ══════════════════════════════════════════════════════════════════════
    #  执行辅助
    # ══════════════════════════════════════════════════════════════════════

    def _aggressive_price(self, price, direction, urgency: str = "normal"):
        if self._pricer is None or self._pricer.last == 0:
            return price
        return self._pricer.price(direction, urgency)

    def _exec_stop_at_tick(self, price, action, reason):
        p = self.params_map
        if self._guard is not None and not self._guard.should_trade():
            return
        pos = self.get_position(p.instrument_id)
        if pos is None:
            return
        actual = pos.net_position
        if actual <= 0:
            return

        for oid in list(self.order_id):
            self.cancel_order(oid)
            if self._entry is not None:
                self._entry.register_cancelled(oid)

        self._slip.set_signal_price(price)
        stop_urgency = ("critical" if action in ("EQUITY_STOP", "CIRCUIT",
                                                  "DAILY_STOP", "FLATTEN")
                        else "urgent")
        sell_price = self._aggressive_price(price, "sell", urgency=stop_urgency)
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=actual, price=sell_price, order_direction="sell",
        )
        if oid is None:
            self.output(f"[TICK_STOP] auto_close return None, 保留 pending")
            return
        self.order_id.add(oid)
        self._om.on_send(oid, actual, sell_price,
                         urgency=stop_urgency, direction="sell", kind="close")

        pnl_pct = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
        self.state_map.last_action = f"{action}[TICK] {pnl_pct:+.2f}%"
        feishu(action.lower(), p.instrument_id,
               f"**[TEST] {action}** tick触发 {actual}手 @ {price:,.1f}\n"
               f"逻辑: {reason}\n盈亏: {pnl_pct:+.2f}%")

        self.avg_price = 0.0
        self.peak_price = 0.0
        self._risk.peak_price = 0.0
        self._risk.trough_price = 0.0
        self._risk._last_trail_minute = None
        # 修复 #7 验证: 止损也写 last_exit_bar_ts
        self._last_exit_bar_ts = self._current_bar_ts()
        self.output(
            f"[RE_ENTRY_GUARD] {action} 触发, "
            f"设 last_exit_bar_ts={self._last_exit_bar_ts}"
        )
        self._save()

    def _save(self):
        state = {
            "peak_price": self.peak_price,
            "avg_price": self.avg_price,
            "trading_day": self._current_td,
            "last_exit_bar_ts": self._last_exit_bar_ts,
            "bar_count": self._bar_count,
            "executor": self._entry.get_state() if self._entry is not None else {},
        }
        state.update(self._risk.get_state())
        save_state(state, name=STRATEGY_NAME)

    def _push_widget(self, kline, sp=0.0):
        try:
            self.widget.recv_kline({
                "kline": kline, "signal_price": sp, **self.main_indicator_data,
            })
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════
    #  回调 — 全部验证修复
    # ══════════════════════════════════════════════════════════════════════

    def on_trade(self, trade: TradeData, log=True):
        """修复 #2 验证: Bug B DIAG + 健壮识别."""
        super().on_trade(trade, log=True)
        self.order_id.discard(trade.order_id)
        self._om.on_fill(trade.order_id)

        # DIAG — 采样实盘真实值
        sample = f"{trade.direction!r}"
        self._diag_direction_samples.append(sample)
        self.state_map.last_direction = sample
        self.state_map.diag_count += 1
        self.output(
            f"[BUG_B_DIAG] direction={sample} offset={trade.offset!r} "
            f"price={trade.price} volume={trade.volume}"
        )

        # 健壮识别 (cover 所有可能的运行时值)
        raw = str(trade.direction).lower()
        direction = "buy" if raw in ("buy", "0", "买") else "sell"

        slip = self._slip.on_fill(trade.price, trade.volume, direction)
        if slip != 0:
            self.output(f"[滑点] {slip:.1f}ticks (direction={direction})")

        oid = trade.order_id
        claimed_by_entry = False
        if self._entry is not None:
            claimed_by_entry = self._entry.on_trade(
                oid, trade.price, trade.volume, datetime.now()
            )

        if not claimed_by_entry:
            self._unknown_oid_count += 1
            if self._unknown_oid_count <= 5 or self._unknown_oid_count % 20 == 0:
                self.output(
                    f"[UNKNOWN_OID] oid={oid} vol={trade.volume} "
                    f"(直接平仓/止损/bar 切换残留), count={self._unknown_oid_count}"
                )

        # 成交价更新 avg_price (多头方向)
        pos = self.get_position(self.params_map.instrument_id)
        actual = pos.net_position if pos else 0
        if direction == "buy" and actual > 0:
            old_pos = max(0, actual - trade.volume)
            if old_pos > 0 and self.avg_price > 0:
                self.avg_price = (
                    (self.avg_price * old_pos + trade.price * trade.volume) / actual
                )
            else:
                self.avg_price = trade.price
            if trade.price > self.peak_price or self.peak_price == 0:
                self.peak_price = trade.price
        elif direction == "sell" and actual <= 0:
            self.avg_price = 0.0
            self.peak_price = 0.0

        self.state_map.net_pos = actual
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.peak_price = round(self.peak_price, 1)
        self._save()
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        """修复 #1 验证: 用 order.cancel_volume, 不用 order.volume (不存在)."""
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)
        self._om.on_cancel(order.order_id)
        # ← Bug A 路径: 访问 .cancel_volume (若用 .volume 会 AttributeError)
        cv = order.cancel_volume
        tv = order.total_volume
        trv = order.traded_volume
        self.output(
            f"[BUG_A_VERIFY] oid={order.order_id} "
            f"cancel_volume={cv} total={tv} traded={trv} → 无 AttributeError"
        )

    def on_error(self, error):
        """修复 #4 验证: throttle_on_error 流控."""
        self.output(f"[错误] {error}")
        feishu("error", self.params_map.instrument_id, f"**[TEST] 异常**: {error}")
        throttle_on_error(self, error)  # ← 若 errCode=0004, 2 秒冷却
