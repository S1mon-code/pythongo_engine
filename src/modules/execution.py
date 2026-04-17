"""Scaled Entry Executor — 像人类交易员一样分仓进场 (2026-04-17, v2 audit-fixed).

设计哲学:
  - 信号 = 本 bar 内的仓位目标, 不是瞬时指令
  - 分仓 (底仓 + 剩余按节奏分批)
  - 盘口挂单 (bid1 for buy, ask1 for sell), 追价重挂(peg-to-bid1 drift)
  - VWAP 作为"便宜价"参考 (price < VWAP 时主动加挂)
  - 兜底催单 (bar 30 分钟后每 5 分钟必须成交 1 手)
  - 止损触发 → 本 bar 锁定放弃剩余
  - 反转意愿强烈可超目标 (触发一次)
  - 动态 urgency 分数驱动穿越 tick 数

状态机:
  IDLE → BOTTOM → OPPORTUNISTIC → FORCE → IDLE
  任意 → LOCKED (止损) → IDLE (新 bar 信号)

Audit v2 修复 (2026-04-17):
  - 所有状态转换点 (on_signal / on_stop_triggered / bar-end) 现在返回
    list[ExecAction] 包括对 pending_oids 的 cancel, 策略层照单执行
  - pending_oids 值改为 dict {vol, price}, 支持真实 peg-to-bid1 追价重挂
  - BOTTOM 过期 (T>deadline_sec) 自动撤老单 + 更高 urgency 重挂, 不再死锁
  - FORCE slot 推进只看时间不看 fill, 避免持续成交永远 peg
  - BOTTOM 初始 wait 自适应 bar 长度 (短 bar 适配)
  - force_cross_min_urgency 参数化 (3 tick vs 7-10 tick 可配)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


# ────────────────────────────────────────────────────────────────────── #


class EntryState(Enum):
    IDLE = "idle"
    BOTTOM = "bottom"
    OPPORTUNISTIC = "opp"
    FORCE = "force"
    LOCKED = "locked"


# ────────────────────────────────────────────────────────────────────── #


@dataclass
class EntryParams:
    """入场执行参数 (策略层传入)."""

    # --- 底仓 ---
    bottom_lots: Optional[int] = 2
    bottom_ratio: Optional[float] = None
    bottom_deadline_sec: int = 300
    bottom_wait_sec: int = 60          # 首次提交前等待(M1 bar 形成); 短 bar 自动缩放

    # --- 机会阶段 ---
    opp_min_submit_interval_sec: int = 10
    max_concurrent_pending: int = 3

    # --- 催单 ---
    force_start_sec: int = 1800
    force_slot_sec: int = 300
    force_peg_sec: int = 120
    force_cross_min_urgency: float = 0.75   # cross 段最低 urgency (0.75=~7-8tick, 0.3=~3tick)

    # --- 超目标 ---
    over_target_enabled: bool = True
    over_target_vwap_pct: float = 0.5
    over_target_forecast: float = 5.0
    over_target_ratio: float = 0.20
    over_target_latest_sec: int = 1500   # bar 进度 >该值 不再触发超目标 (剩余时间不够消化)

    # --- Urgency ---
    max_entry_cross_ticks: int = 10
    urgency_weights: tuple = (0.40, 0.30, 0.15, 0.15)

    # --- Peg (盘口追价) ---
    peg_tick_threshold: int = 1         # bid1 偏离 >= N tick 触发 peg 重挂

    # --- 未来扩展 (本期预留) ---
    max_pov_ratio: Optional[float] = None
    visible_lots: Optional[int] = None


# ────────────────────────────────────────────────────────────────────── #


@dataclass
class ExecAction:
    op: str                       # "submit" | "cancel" | "cancel_all" | "feishu"
    vol: int = 0
    price: float = 0.0
    direction: str = ""
    urgency_score: float = 0.0
    kind: str = "open"
    oid: object = None
    note: str = ""


# ────────────────────────────────────────────────────────────────────── #


@dataclass
class _PendingOrder:
    vol: int
    price: float   # 提交时的挂单价, 用于 peg 比较


@dataclass
class _State:
    state: EntryState = EntryState.IDLE
    target: int = 0
    target_original: int = 0
    filled: int = 0
    direction: str = ""
    bar_start: Optional[datetime] = None
    bar_total_sec: int = 3600
    bottom_lots_actual: int = 0
    bottom_submitted: bool = False
    bottom_filled: bool = False
    bottom_overdue_escalated: bool = False
    over_target_triggered: bool = False
    last_submit_ts: Optional[datetime] = None
    force_slot_start: Optional[datetime] = None
    force_slot_crossed: bool = False
    # oid -> _PendingOrder
    pending_oids: dict = field(default_factory=dict)
    # Feishu 节点去重
    feishu_bottom_sent: bool = False
    feishu_opp_first_sent: bool = False
    feishu_force_sent: bool = False


# ────────────────────────────────────────────────────────────────────── #


class ScaledEntryExecutor:
    """Bar 级分仓进场状态机 (audit v2)."""

    def __init__(self, params: EntryParams):
        self.p = params
        self.s = _State()

    # ================================================================== #
    # 外部接口 — 所有状态转换方法都返回 list[ExecAction]
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
        """新 bar 信号. Reconcile 到新 target, 重置状态.

        如果当前有 pending_oids, 先 emit cancel actions 再重置。
        """
        if direction not in ("buy", "sell"):
            return []

        # 先清理旧 pending (mid-bar signal or LOCKED → new signal)
        actions = self._cancel_all_pending()

        # Reconcile 方向
        if direction == "buy" and current_position < 0:
            return actions
        if direction == "sell" and current_position > 0:
            return actions

        abs_pos = abs(current_position)
        if abs_pos >= target:
            # 满仓或超, 不入场
            self.s = _State()
            self.s.state = EntryState.IDLE
            self.s.target = target
            self.s.target_original = target
            self.s.direction = direction
            self.s.bar_start = now
            self.s.bar_total_sec = bar_total_sec
            return actions

        delta = target - abs_pos
        self.s = _State()
        self.s.state = EntryState.BOTTOM
        self.s.target = delta
        self.s.target_original = delta
        self.s.direction = direction
        self.s.bar_start = now
        self.s.bar_total_sec = bar_total_sec
        self.s.bottom_lots_actual = self._compute_bottom_lots(delta)
        return actions

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

        # Bar 结束: 清理所有 pending, 回 IDLE
        if elapsed >= self.s.bar_total_sec:
            actions = self._cancel_all_pending()
            self.s.state = EntryState.IDLE
            return actions

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
        """成交回报. 更新 filled."""
        if oid in self.s.pending_oids:
            order = self.s.pending_oids[oid]
            registered = order.vol
            actual_vol = min(registered, vol)
            self.s.filled += actual_vol
            remaining_in_order = registered - actual_vol
            if remaining_in_order > 0:
                order.vol = remaining_in_order
            else:
                del self.s.pending_oids[oid]

        if (
            self.s.state == EntryState.BOTTOM
            and self.s.filled >= self.s.bottom_lots_actual
        ):
            self.s.bottom_filled = True

    def on_stop_triggered(self, now: datetime) -> list[ExecAction]:
        """止损触发. 清理 pending + 锁定本 bar."""
        actions = self._cancel_all_pending()
        self.s.state = EntryState.LOCKED
        return actions

    def register_pending(self, oid, vol: int, price: float = 0.0) -> None:
        """策略层下单成功后调用."""
        if oid is not None and vol > 0:
            self.s.pending_oids[oid] = _PendingOrder(vol=vol, price=price)

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
        return sum(o.vol for o in self.s.pending_oids.values())

    @property
    def remaining(self) -> int:
        return max(0, self.s.target - self.s.filled - self.pending_vol)

    @property
    def state(self) -> EntryState:
        return self.s.state

    @property
    def pending_oids(self) -> dict:
        """只读视图, key=oid, value=_PendingOrder."""
        return dict(self.s.pending_oids)

    def progress_str(self) -> str:
        return (
            f"{self.s.state.value} {self.s.filled}/{self.s.target} "
            f"pend={self.pending_vol} over={self.s.over_target_triggered}"
        )

    # ================================================================== #
    # Helpers — cancel_all
    # ================================================================== #

    def _cancel_all_pending(self) -> list[ExecAction]:
        """生成所有 pending 的 cancel action, 清 pending_oids."""
        actions = []
        for oid in list(self.s.pending_oids.keys()):
            actions.append(ExecAction(op="cancel", oid=oid))
        self.s.pending_oids.clear()
        return actions

    # ================================================================== #
    # compute_bottom_lots / bottom_wait
    # ================================================================== #

    def _compute_bottom_lots(self, target: int) -> int:
        if target <= 0:
            return 0
        if self.p.bottom_lots is not None:
            return min(self.p.bottom_lots, target)
        if self.p.bottom_ratio is not None:
            return max(1, min(target, int(math.ceil(target * self.p.bottom_ratio))))
        return min(2, target)

    def _effective_bottom_wait_sec(self) -> int:
        """bar-aware 底仓等待. 短 bar 自动缩放 (bar*5%, clamp 到 [5, bottom_wait_sec])."""
        scaled = max(5, self.s.bar_total_sec // 20)
        return min(self.p.bottom_wait_sec, scaled)

    # ================================================================== #
    # Urgency
    # ================================================================== #

    def _compute_urgency(
        self,
        *,
        elapsed: float,
        forecast: float,
        last_price: float,
        vwap_value: float,
    ) -> float:
        w_time, w_deficit, w_signal, w_opp = self.p.urgency_weights

        # 时间压力
        if self.s.state == EntryState.FORCE and self.s.force_slot_start is not None:
            slot_elapsed = (elapsed - (self.s.force_slot_start - self.s.bar_start).total_seconds())
            slot_progress = max(0.0, min(slot_elapsed / self.p.force_slot_sec, 1.0))
            time_pressure = 0.6 + 0.4 * slot_progress
        elif self.s.state == EntryState.BOTTOM and elapsed > self.p.bottom_deadline_sec:
            overdue_sec = elapsed - self.p.bottom_deadline_sec
            time_pressure = min(0.7 + overdue_sec / 300.0 * 0.3, 1.0)
        else:
            time_pressure = min(elapsed / self.s.bar_total_sec, 1.0)

        deficit = (self.s.target - self.s.filled) / self.s.target if self.s.target > 0 else 0
        deficit = max(0.0, min(deficit, 1.0))

        signal_strength = min(max(forecast, 0) / 10.0, 1.0)

        if vwap_value > 0 and last_price > 0:
            if self.s.direction == "buy":
                diff_pct = (vwap_value - last_price) / vwap_value
            else:
                diff_pct = (last_price - vwap_value) / vwap_value
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
        # 等待(bar-aware)
        wait_sec = self._effective_bottom_wait_sec()
        if elapsed < wait_sec:
            return []

        # 底仓已成, 转 OPPORTUNISTIC
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

                if not self.s.feishu_bottom_sent:
                    actions.append(ExecAction(
                        op="feishu",
                        note=f"**ENTRY BOTTOM** {vol}手 {self.s.direction} @ "
                             f"{price:.1f} urgency={urgency:.2f}",
                    ))
                    self.s.feishu_bottom_sent = True
            return actions

        # BOTTOM 过期 (T > deadline): 撤老单 + 强制 resubmit 更高 urgency
        if elapsed >= self.p.bottom_deadline_sec and not self.s.bottom_overdue_escalated:
            actions.extend(self._cancel_all_pending())
            vol_needed = self.s.bottom_lots_actual - self.s.filled
            if vol_needed > 0:
                price = self._price_from_urgency(
                    self.s.direction, urgency, bid1, ask1, tick_size, last_price,
                )
                actions.append(ExecAction(
                    op="submit", vol=vol_needed, price=price,
                    direction=self.s.direction, urgency_score=urgency, kind="open",
                ))
                self.s.last_submit_ts = now
                actions.append(ExecAction(
                    op="feishu",
                    note=f"**ENTRY BOTTOM OVERDUE** 强制 urgency={urgency:.2f} "
                         f"@ {price:.1f}",
                ))
            self.s.bottom_overdue_escalated = True
            return actions

        # Peg check (bid1 漂移)
        actions.extend(self._peg_pending(
            bid1, ask1, tick_size, last_price, urgency,
        ))
        return actions

    def _drive_opportunistic(
        self, *, now, elapsed, last_price, bid1, ask1, tick_size,
        vwap_value, forecast,
    ) -> list[ExecAction]:
        # FORCE 启动
        if elapsed >= self.p.force_start_sec:
            self.s.state = EntryState.FORCE
            self.s.force_slot_start = now
            self.s.force_slot_crossed = False
            actions: list[ExecAction] = []
            if not self.s.feishu_force_sent:
                actions.append(ExecAction(
                    op="feishu",
                    note=f"**ENTRY FORCE** 催单阶段 remaining={self.remaining}",
                ))
                self.s.feishu_force_sent = True
            actions.extend(self._drive_force(
                now=now, elapsed=elapsed, last_price=last_price,
                bid1=bid1, ask1=ask1, tick_size=tick_size,
                vwap_value=vwap_value, forecast=forecast,
            ))
            return actions

        if self.remaining <= 0:
            return []

        actions: list[ExecAction] = []
        urgency = self._compute_urgency(
            elapsed=elapsed, forecast=forecast,
            last_price=last_price, vwap_value=vwap_value,
        )

        # 超目标 (不在 bar 末段触发,避免 catch-up 不及)
        if elapsed < self.p.over_target_latest_sec:
            self._check_over_target(last_price, vwap_value, forecast)

        # Peg 漂移
        actions.extend(self._peg_pending(
            bid1, ask1, tick_size, last_price, urgency,
        ))

        # 发新单
        interval_ok = (
            self.s.last_submit_ts is None
            or (now - self.s.last_submit_ts).total_seconds() >= self.p.opp_min_submit_interval_sec
        )
        has_cap = len(self.s.pending_oids) < self.p.max_concurrent_pending

        cheap = False
        if vwap_value > 0 and last_price > 0:
            if self.s.direction == "buy":
                cheap = last_price < vwap_value
            else:
                cheap = last_price > vwap_value

        should_submit = (
            interval_ok and has_cap and self.remaining > 0
            and (cheap or urgency > 0.5)
        )

        if should_submit:
            price = self._price_from_urgency(
                self.s.direction, urgency, bid1, ask1, tick_size, last_price,
            )
            actions.append(ExecAction(
                op="submit", vol=1, price=price,
                direction=self.s.direction, urgency_score=urgency, kind="open",
            ))
            self.s.last_submit_ts = now

            if not self.s.feishu_opp_first_sent:
                actions.append(ExecAction(
                    op="feishu",
                    note=f"**ENTRY OPP 首笔** @ {price:.1f} urgency={urgency:.2f}",
                ))
                self.s.feishu_opp_first_sent = True

        return actions

    def _drive_force(
        self, *, now, elapsed, last_price, bid1, ask1, tick_size,
        vwap_value, forecast,
    ) -> list[ExecAction]:
        if self.remaining <= 0:
            # 完成: cleanup + 回 IDLE
            actions = self._cancel_all_pending()
            self.s.state = EntryState.IDLE
            return actions

        if self.s.force_slot_start is None:
            self.s.force_slot_start = now
            self.s.force_slot_crossed = False

        slot_elapsed = (now - self.s.force_slot_start).total_seconds()

        # Slot 过期 (时间驱动, 不看 fill): 开新 slot
        if slot_elapsed >= self.p.force_slot_sec:
            self.s.force_slot_start = now
            self.s.force_slot_crossed = False
            slot_elapsed = 0.0

        urgency = self._compute_urgency(
            elapsed=elapsed, forecast=forecast,
            last_price=last_price, vwap_value=vwap_value,
        )

        actions: list[ExecAction] = []

        if slot_elapsed < self.p.force_peg_sec:
            # Peg 段
            actions.extend(self._peg_pending(
                bid1, ask1, tick_size, last_price, urgency,
            ))
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
            # Cross 段
            if not self.s.force_slot_crossed:
                actions.extend(self._cancel_all_pending())
                cross_urgency = max(urgency, self.p.force_cross_min_urgency)
                if self.remaining > 0:
                    price = self._price_from_urgency(
                        self.s.direction, cross_urgency,
                        bid1, ask1, tick_size, last_price,
                    )
                    actions.append(ExecAction(
                        op="submit", vol=1, price=price,
                        direction=self.s.direction,
                        urgency_score=cross_urgency, kind="open",
                    ))
                    self.s.last_submit_ts = now
                self.s.force_slot_crossed = True

        return actions

    # ================================================================== #
    # Peg + pricing
    # ================================================================== #

    def _peg_pending(
        self, bid1: float, ask1: float, tick_size: float,
        last_price: float, urgency: float,
    ) -> list[ExecAction]:
        """bid1/ask1 漂移 peg_tick_threshold 以上时撤老单重挂."""
        actions = []
        if tick_size <= 0 or not self.s.pending_oids:
            return actions

        target_price = self._price_from_urgency(
            self.s.direction, urgency, bid1, ask1, tick_size, last_price,
        )

        threshold = self.p.peg_tick_threshold * tick_size
        for oid, order in list(self.s.pending_oids.items()):
            if order.price <= 0:
                continue  # 无原价信息, 跳过
            drift = abs(target_price - order.price)
            if drift >= threshold:
                # 撤 + 重挂
                actions.append(ExecAction(op="cancel", oid=oid))
                actions.append(ExecAction(
                    op="submit", vol=order.vol, price=target_price,
                    direction=self.s.direction, urgency_score=urgency, kind="open",
                ))
                # 本地先删, 等策略层 register_cancelled / register_pending 覆盖
                del self.s.pending_oids[oid]

        return actions

    def _price_from_urgency(
        self, direction: str, urgency: float,
        bid1: float, ask1: float, tick_size: float, last_price: float,
    ) -> float:
        """Inline 实现, 和 pricing.price_with_urgency_score 等价."""
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
        if direction == "buy":
            return last_price + ticks * tick_size
        return last_price - ticks * tick_size

    def _check_over_target(
        self, last_price: float, vwap_value: float, forecast: float,
    ) -> None:
        if not self.p.over_target_enabled:
            return
        if self.s.over_target_triggered:
            return
        if vwap_value <= 0 or last_price <= 0:
            return

        if self.s.direction == "buy":
            threshold = vwap_value * (1 - self.p.over_target_vwap_pct / 100)
            condition_price = last_price < threshold
        else:
            threshold = vwap_value * (1 + self.p.over_target_vwap_pct / 100)
            condition_price = last_price > threshold

        condition_signal = forecast > self.p.over_target_forecast

        if condition_price and condition_signal:
            extra = max(1, int(math.floor(self.s.target_original * self.p.over_target_ratio)))
            self.s.target += extra
            self.s.over_target_triggered = True
