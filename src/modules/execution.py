"""Scaled Entry Executor — 像人类交易员一样分仓进场 (2026-04-17).

设计哲学:
  - 信号 = 本 bar 内的仓位目标, 不是瞬时指令
  - 分仓 (底仓 + 剩余按节奏分批)
  - 盘口挂单 (bid1 for buy, ask1 for sell), 追价重挂
  - VWAP 作为"便宜价"参考 (price < VWAP 时主动加挂)
  - 兜底催单 (bar 30 分钟后每 5 分钟必须成交 1 手)
  - 止损触发 → 本 bar 锁定放弃剩余
  - 反转意愿强烈可超目标 (触发一次)
  - 动态 urgency 分数驱动穿越 tick 数

状态机:
  IDLE → BOTTOM → OPPORTUNISTIC → FORCE → COMPLETE → IDLE
  任意 → LOCKED (止损) → IDLE (新 bar 信号)

关键设计决策:
  - 记账用 `remaining = target - filled - pending_vol` 动态算, 避免 V8 VWAP
    的"发单成功就扣 remaining, cancel 不回补"bug
  - Executor 返回 ExecAction 列表, 策略层执行, 状态和行为分离便于单测
  - 所有 state 封装在 Executor 对象内, 策略不直接访问内部字段
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


# ────────────────────────────────────────────────────────────────────── #
#  状态枚举
# ────────────────────────────────────────────────────────────────────── #


class EntryState(Enum):
    IDLE = "idle"
    BOTTOM = "bottom"
    OPPORTUNISTIC = "opp"
    FORCE = "force"
    LOCKED = "locked"


# ────────────────────────────────────────────────────────────────────── #
#  参数
# ────────────────────────────────────────────────────────────────────── #


@dataclass
class EntryParams:
    """入场执行参数 (策略层传入)."""

    # --- 底仓 ---
    bottom_lots: Optional[int] = 2           # 固定手数 (和 bottom_ratio 二选一)
    bottom_ratio: Optional[float] = None     # target 的比例
    bottom_deadline_sec: int = 300           # 5 min 未成就强制(urgency 拔高)

    # --- 机会阶段 ---
    opp_min_submit_interval_sec: int = 10
    max_concurrent_pending: int = 3

    # --- 催单 ---
    force_start_sec: int = 1800              # 30 min
    force_slot_sec: int = 300                # 5 min
    force_peg_sec: int = 120                 # slot 前 2 min peg

    # --- 超目标 ---
    over_target_enabled: bool = True
    over_target_vwap_pct: float = 0.5        # price < VWAP × (1 - 0.5/100)
    over_target_forecast: float = 5.0
    over_target_ratio: float = 0.20

    # --- Urgency ---
    max_entry_cross_ticks: int = 10
    # (time, deficit, signal, opportunity)
    urgency_weights: tuple = (0.40, 0.30, 0.15, 0.15)

    # --- 未来扩展 (本期不启用) ---
    max_pov_ratio: Optional[float] = None
    visible_lots: Optional[int] = None


# ────────────────────────────────────────────────────────────────────── #
#  Action(executor 发回给策略层的动作)
# ────────────────────────────────────────────────────────────────────── #


@dataclass
class ExecAction:
    """Executor 让策略层执行的一次动作."""
    op: str                       # "submit" | "cancel" | "cancel_all" | "feishu"
    vol: int = 0
    price: float = 0.0
    direction: str = ""           # "buy" / "sell"
    urgency_score: float = 0.0    # [0.0, 1.0]
    kind: str = "open"            # "open" (send_order) / "close" (auto_close_position)
    oid: object = None            # for cancel
    note: str = ""                # for feishu payload


# ────────────────────────────────────────────────────────────────────── #
#  内部 state data
# ────────────────────────────────────────────────────────────────────── #


@dataclass
class _State:
    state: EntryState = EntryState.IDLE
    target: int = 0
    target_original: int = 0             # 超目标前
    filled: int = 0
    direction: str = ""
    bar_start: Optional[datetime] = None
    bar_total_sec: int = 3600
    bottom_lots_actual: int = 0
    bottom_submitted: bool = False
    bottom_filled: bool = False
    over_target_triggered: bool = False
    last_submit_ts: Optional[datetime] = None
    force_slot_start: Optional[datetime] = None  # 当前 slot 起点
    force_slot_crossed: bool = False             # 本 slot 是否已进 cross 段
    # pending_oids: oid -> vol
    pending_oids: dict = field(default_factory=dict)
    # 本 slot 已成交的手数, 用于判断是否该开下一个 slot
    slot_filled_lots: int = 0


# ────────────────────────────────────────────────────────────────────── #
#  Executor
# ────────────────────────────────────────────────────────────────────── #


class ScaledEntryExecutor:
    """Bar 级分仓进场状态机.

    用法:
        executor = ScaledEntryExecutor(EntryParams(bottom_lots=2))

        # 信号触发:
        actions = executor.on_signal(
            target=6, direction="buy",
            now=datetime.now(), current_position=0,
            forecast=9.0, bar_total_sec=3600,
        )

        # 每 tick:
        actions = executor.on_tick(
            now=now, last_price=price,
            bid1=bid, ask1=ask, tick_size=5.0,
            vwap_value=vwap, forecast=8.5, current_position=2,
        )
        for a in actions:
            strategy.apply_entry_action(a)

        # 成交回报:
        executor.on_trade(oid, price, vol, now)

        # 止损触发:
        executor.on_stop_triggered(now)
    """

    def __init__(self, params: EntryParams):
        self.p = params
        self.s = _State()
        # For pricer-less mode: executor returns urgency score,策略层用 pricer.price_with_urgency_score 算价
        # For testability: we compute price inline using pricer-like functions, 也接受注入

    # ================================================================== #
    # 外部接口
    # ================================================================== #

    def on_signal(
        self,
        *,
        target: int,
        direction: str,
        now: datetime,
        current_position: int,
        forecast: float,
        bar_total_sec: int = 3600,
    ) -> list[ExecAction]:
        """新 bar 信号. Reconcile 到新 target, 重置状态."""
        if direction not in ("buy", "sell"):
            return []

        # Reconcile
        abs_pos = abs(current_position)
        if direction == "buy" and current_position < 0:
            # 反向持仓, 不启动 entry (策略层应先平)
            return []
        if direction == "sell" and current_position > 0:
            return []

        if abs_pos >= target:
            # 已满仓或超, 不入场; 等新 bar 重算
            self.s = _State()
            self.s.state = EntryState.IDLE
            self.s.target = target
            self.s.target_original = target
            self.s.direction = direction
            self.s.bar_start = now
            self.s.bar_total_sec = bar_total_sec
            return []

        # 从 IDLE/LOCKED 启动新 bar 窗口
        delta = target - abs_pos
        self.s = _State()
        self.s.state = EntryState.BOTTOM
        self.s.target = delta                # 这个 bar 要建的 delta 仓位
        self.s.target_original = delta
        self.s.direction = direction
        self.s.bar_start = now
        self.s.bar_total_sec = bar_total_sec
        self.s.bottom_lots_actual = self._compute_bottom_lots(delta)
        return []

    def on_tick(
        self,
        *,
        now: datetime,
        last_price: float,
        bid1: float,
        ask1: float,
        tick_size: float,
        vwap_value: float,
        forecast: float,
        current_position: int,
    ) -> list[ExecAction]:
        """每 tick 驱动. 返回策略层需要执行的 actions."""
        if self.s.state in (EntryState.IDLE, EntryState.LOCKED):
            return []
        if self.s.bar_start is None:
            return []

        elapsed = (now - self.s.bar_start).total_seconds()

        # Bar 结束了, 该 executor 该退场
        if elapsed >= self.s.bar_total_sec:
            self.s.state = EntryState.IDLE
            return []

        # 状态分发
        if self.s.state == EntryState.BOTTOM:
            return self._drive_bottom(
                now=now, elapsed=elapsed, last_price=last_price,
                bid1=bid1, ask1=ask1, tick_size=tick_size,
                vwap_value=vwap_value, forecast=forecast,
            )
        if self.s.state == EntryState.OPPORTUNISTIC:
            return self._drive_opportunistic(
                now=now, elapsed=elapsed, last_price=last_price,
                bid1=bid1, ask1=ask1, tick_size=tick_size,
                vwap_value=vwap_value, forecast=forecast,
            )
        if self.s.state == EntryState.FORCE:
            return self._drive_force(
                now=now, elapsed=elapsed, last_price=last_price,
                bid1=bid1, ask1=ask1, tick_size=tick_size,
                vwap_value=vwap_value, forecast=forecast,
            )
        return []

    def on_trade(self, oid, price: float, vol: int, now: datetime) -> None:
        """成交回报. 更新 filled 和 pending_vol."""
        if oid in self.s.pending_oids:
            registered_vol = self.s.pending_oids[oid]
            actual_vol = min(registered_vol, vol)
            self.s.filled += actual_vol
            self.s.slot_filled_lots += actual_vol
            # 部分成交: pending 扣除本次, 全部成交才 pop
            remaining_in_order = registered_vol - actual_vol
            if remaining_in_order > 0:
                self.s.pending_oids[oid] = remaining_in_order
            else:
                del self.s.pending_oids[oid]
        else:
            # 未登记的 oid (可能来自策略层其他路径), 忽略
            pass

        # 检查是否 bottom 成交
        if (
            self.s.state == EntryState.BOTTOM
            and self.s.filled >= self.s.bottom_lots_actual
        ):
            self.s.bottom_filled = True

    def on_stop_triggered(self, now: datetime) -> None:
        """止损触发. 锁定本 bar 剩余仓位."""
        self.s.state = EntryState.LOCKED

    def register_pending(self, oid, vol: int) -> None:
        """策略层下单成功后调用, 登记 pending."""
        if oid is not None and vol > 0:
            self.s.pending_oids[oid] = vol

    def register_cancelled(self, oid) -> None:
        """策略层撤单后调用."""
        self.s.pending_oids.pop(oid, None)

    # ================================================================== #
    # 查询
    # ================================================================== #

    @property
    def filled(self) -> int:
        return self.s.filled

    @property
    def target(self) -> int:
        return self.s.target

    @property
    def pending_vol(self) -> int:
        return sum(self.s.pending_oids.values())

    @property
    def remaining(self) -> int:
        """target - filled - pending_vol (动态计算, 避免记账错位)."""
        return max(0, self.s.target - self.s.filled - self.pending_vol)

    @property
    def state(self) -> EntryState:
        return self.s.state

    @property
    def pending_oids(self) -> dict:
        return dict(self.s.pending_oids)

    def progress_str(self) -> str:
        return (
            f"{self.s.state.value} {self.s.filled}/{self.s.target} "
            f"pend={self.pending_vol} over={self.s.over_target_triggered}"
        )

    # ================================================================== #
    # 内部:compute_bottom_lots
    # ================================================================== #

    def _compute_bottom_lots(self, target: int) -> int:
        """依据 params 计算底仓手数."""
        if target <= 0:
            return 0
        if self.p.bottom_lots is not None:
            return min(self.p.bottom_lots, target)
        if self.p.bottom_ratio is not None:
            return max(1, min(target, int(math.ceil(target * self.p.bottom_ratio))))
        return min(2, target)  # 默认 2 手

    # ================================================================== #
    # 内部:compute_urgency
    # ================================================================== #

    def _compute_urgency(
        self,
        *,
        elapsed: float,
        forecast: float,
        last_price: float,
        vwap_value: float,
    ) -> float:
        """动态 urgency 分数 [0, 1]."""
        w_time, w_deficit, w_signal, w_opp = self.p.urgency_weights

        # 1) 时间压力
        if self.s.state == EntryState.FORCE and self.s.force_slot_start is not None:
            slot_elapsed = (elapsed - (self.s.force_slot_start - self.s.bar_start).total_seconds())
            slot_progress = max(0.0, min(slot_elapsed / self.p.force_slot_sec, 1.0))
            time_pressure = 0.6 + 0.4 * slot_progress
        elif self.s.state == EntryState.BOTTOM and elapsed > self.p.bottom_deadline_sec:
            # 底仓超时: urgency 基线 0.7+
            overdue_sec = elapsed - self.p.bottom_deadline_sec
            time_pressure = min(0.7 + overdue_sec / 300.0 * 0.3, 1.0)
        else:
            time_pressure = min(elapsed / self.s.bar_total_sec, 1.0)

        # 2) 持仓缺口
        deficit = (self.s.target - self.s.filled) / self.s.target if self.s.target > 0 else 0
        deficit = max(0.0, min(deficit, 1.0))

        # 3) 信号强度
        signal_strength = min(max(forecast, 0) / 10.0, 1.0)

        # 4) 价格机会
        if vwap_value > 0 and last_price > 0:
            diff_pct = (vwap_value - last_price) / vwap_value
            opportunity = max(0.0, min(diff_pct * 100, 1.0))
        else:
            opportunity = 0.0

        urgency = (
            time_pressure * w_time
            + deficit * w_deficit
            + signal_strength * w_signal
            + opportunity * w_opp
        )
        return max(0.0, min(1.0, urgency))

    # ================================================================== #
    # Stage drivers
    # ================================================================== #

    def _drive_bottom(
        self, *, now, elapsed, last_price, bid1, ask1, tick_size,
        vwap_value, forecast,
    ) -> list[ExecAction]:
        # T<60s: 等 M1 bar 形成
        if elapsed < 60:
            return []

        # 底仓已成, 进入 OPPORTUNISTIC
        if self.s.bottom_filled or self.s.filled >= self.s.bottom_lots_actual:
            self.s.state = EntryState.OPPORTUNISTIC
            return self._drive_opportunistic(
                now=now, elapsed=elapsed, last_price=last_price,
                bid1=bid1, ask1=ask1, tick_size=tick_size,
                vwap_value=vwap_value, forecast=forecast,
            )

        urgency = self._compute_urgency(
            elapsed=elapsed, forecast=forecast,
            last_price=last_price, vwap_value=vwap_value,
        )
        actions: list[ExecAction] = []

        # 首次发底仓
        if not self.s.bottom_submitted:
            vol = self.s.bottom_lots_actual
            if vol > 0:
                price = self._price_from_urgency(
                    self.s.direction, urgency, bid1, ask1, tick_size, last_price,
                )
                actions.append(ExecAction(
                    op="submit", vol=vol, price=price,
                    direction=self.s.direction, urgency_score=urgency, kind="open",
                ))
                self.s.bottom_submitted = True
                self.s.last_submit_ts = now
            return actions

        # 已发底仓, 检查 peg (bid 漂移)
        actions.extend(self._peg_pending(
            bid1, ask1, tick_size, last_price, urgency,
        ))
        return actions

    def _drive_opportunistic(
        self, *, now, elapsed, last_price, bid1, ask1, tick_size,
        vwap_value, forecast,
    ) -> list[ExecAction]:
        # FORCE 阶段启动
        if elapsed >= self.p.force_start_sec:
            self.s.state = EntryState.FORCE
            self.s.force_slot_start = now
            self.s.force_slot_crossed = False
            self.s.slot_filled_lots = 0
            return self._drive_force(
                now=now, elapsed=elapsed, last_price=last_price,
                bid1=bid1, ask1=ask1, tick_size=tick_size,
                vwap_value=vwap_value, forecast=forecast,
            )

        # 全部完成
        if self.remaining <= 0:
            return []

        actions: list[ExecAction] = []
        urgency = self._compute_urgency(
            elapsed=elapsed, forecast=forecast,
            last_price=last_price, vwap_value=vwap_value,
        )

        # 超目标检查 (只触发一次)
        self._check_over_target(last_price, vwap_value, forecast)

        # bid 漂移 peg
        actions.extend(self._peg_pending(
            bid1, ask1, tick_size, last_price, urgency,
        ))

        # 发新单判断
        interval_ok = (
            self.s.last_submit_ts is None
            or (now - self.s.last_submit_ts).total_seconds() >= self.p.opp_min_submit_interval_sec
        )
        has_cap = len(self.s.pending_oids) < self.p.max_concurrent_pending

        # 触发: price<VWAP 或 urgency > 0.5 (紧张时主动追)
        cheap = (
            vwap_value > 0 and last_price > 0
            and (
                (self.s.direction == "buy" and last_price < vwap_value)
                or (self.s.direction == "sell" and last_price > vwap_value)
            )
        )
        should_submit = interval_ok and has_cap and self.remaining > 0 and (cheap or urgency > 0.5)

        if should_submit:
            price = self._price_from_urgency(
                self.s.direction, urgency, bid1, ask1, tick_size, last_price,
            )
            actions.append(ExecAction(
                op="submit", vol=1, price=price,
                direction=self.s.direction, urgency_score=urgency, kind="open",
            ))
            self.s.last_submit_ts = now

        return actions

    def _drive_force(
        self, *, now, elapsed, last_price, bid1, ask1, tick_size,
        vwap_value, forecast,
    ) -> list[ExecAction]:
        # 完成
        if self.remaining <= 0:
            self.s.state = EntryState.IDLE
            return []

        actions: list[ExecAction] = []

        # Slot 管理: 如果本 slot 已经成交 1+ 手, 开始下一 slot
        if self.s.slot_filled_lots >= 1:
            self.s.force_slot_start = now
            self.s.force_slot_crossed = False
            self.s.slot_filled_lots = 0

        if self.s.force_slot_start is None:
            self.s.force_slot_start = now
            self.s.force_slot_crossed = False

        slot_elapsed = (now - self.s.force_slot_start).total_seconds()
        # 当前 slot 过期: 开新 slot
        if slot_elapsed >= self.p.force_slot_sec:
            self.s.force_slot_start = now
            self.s.force_slot_crossed = False
            slot_elapsed = 0

        urgency = self._compute_urgency(
            elapsed=elapsed, forecast=forecast,
            last_price=last_price, vwap_value=vwap_value,
        )

        # bid 漂移 peg (slot 前 2 min)
        if slot_elapsed < self.p.force_peg_sec:
            actions.extend(self._peg_pending(
                bid1, ask1, tick_size, last_price, urgency,
            ))
            # 如果 slot 开头没有 pending, 发一个
            if len(self.s.pending_oids) == 0 and self.remaining > 0:
                price = self._price_from_urgency(
                    self.s.direction, urgency, bid1, ask1, tick_size, last_price,
                )
                actions.append(ExecAction(
                    op="submit", vol=1, price=price,
                    direction=self.s.direction, urgency_score=urgency, kind="open",
                ))
                self.s.last_submit_ts = now
        else:
            # Slot 后 3 min: 穿盘口
            if not self.s.force_slot_crossed:
                # 撤所有 pending, 重挂 urgency 更高
                for oid in list(self.s.pending_oids.keys()):
                    actions.append(ExecAction(op="cancel", oid=oid))
                # Urgency 提升到至少 0.75 (穿 7-8 tick)
                cross_urgency = max(urgency, 0.75)
                if self.remaining > 0:
                    price = self._price_from_urgency(
                        self.s.direction, cross_urgency, bid1, ask1, tick_size, last_price,
                    )
                    actions.append(ExecAction(
                        op="submit", vol=1, price=price,
                        direction=self.s.direction, urgency_score=cross_urgency, kind="open",
                    ))
                    self.s.last_submit_ts = now
                self.s.force_slot_crossed = True

        return actions

    # ================================================================== #
    # Helpers
    # ================================================================== #

    def _price_from_urgency(
        self, direction: str, urgency: float,
        bid1: float, ask1: float, tick_size: float, last_price: float,
    ) -> float:
        """Inline 实现 pricing.price_with_urgency_score (供 executor 直接用,不依赖 pricer).

        实际集成时策略层可选择用 pricer.price_with_urgency_score, 两者等价。
        """
        urgency = max(0.0, min(1.0, urgency))
        ticks = int(round(urgency * self.p.max_entry_cross_ticks))
        has_book = bid1 > 0 and ask1 > 0

        if ticks == 0:
            if direction == "buy":
                return bid1 if has_book else last_price
            return ask1 if has_book else last_price

        if has_book:
            if direction == "buy":
                return ask1 + ticks * tick_size
            return bid1 - ticks * tick_size
        # Fallback
        if direction == "buy":
            return last_price + ticks * tick_size
        return last_price - ticks * tick_size

    def _peg_pending(
        self, bid1: float, ask1: float, tick_size: float,
        last_price: float, urgency: float,
    ) -> list[ExecAction]:
        """bid1 漂移时撤老单重挂.

        简化策略: urgency=0 时只有 peg 才撤重挂 (挂单价 != 当前 bid1)。
        urgency > 0 (已穿盘口) 时, 订单是在对手盘口往前追的, 不触发 peg。
        """
        # 目前简化为不主动 peg (避免过度撤重挂的复杂 bookkeeping)
        # 真实 peg 需要追踪每个 oid 的挂单价和提交时 bid1 状态, 复杂度较高
        # V1 版本依靠 escalator/force 机制自然更新, 这里保留方法供未来扩展
        return []

    def _check_over_target(
        self, last_price: float, vwap_value: float, forecast: float,
    ) -> None:
        """超目标加仓触发 (只一次)."""
        if not self.p.over_target_enabled:
            return
        if self.s.over_target_triggered:
            return
        if vwap_value <= 0 or last_price <= 0:
            return

        threshold = vwap_value * (1 - self.p.over_target_vwap_pct / 100)
        condition_price = last_price < threshold if self.s.direction == "buy" else last_price > vwap_value * (1 + self.p.over_target_vwap_pct / 100)
        condition_signal = forecast > self.p.over_target_forecast

        if condition_price and condition_signal:
            extra = max(1, int(math.floor(self.s.target_original * self.p.over_target_ratio)))
            self.s.target += extra
            self.s.over_target_triggered = True
