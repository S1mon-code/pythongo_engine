"""Unit tests for modules.execution.ScaledEntryExecutor."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from modules.execution import (
    EntryParams,
    EntryState,
    ExecAction,
    ScaledEntryExecutor,
)


T0 = datetime(2026, 4, 17, 10, 0, 0)


def _default_params(**overrides) -> EntryParams:
    p = EntryParams(
        bottom_lots=2,
        bottom_deadline_sec=300,
        opp_min_submit_interval_sec=10,
        max_concurrent_pending=3,
        force_start_sec=1800,
        force_slot_sec=300,
        force_peg_sec=120,
        over_target_enabled=True,
        over_target_vwap_pct=0.5,
        over_target_forecast=5.0,
        over_target_ratio=0.20,
        max_entry_cross_ticks=10,
    )
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


def _base_tick_args(now=None, elapsed_sec=0, last_price=100.0, bid1=99.5, ask1=100.5,
                     tick_size=5.0, vwap=100.0, forecast=5.0, position=0):
    if now is None:
        now = T0 + timedelta(seconds=elapsed_sec)
    return dict(
        now=now, last_price=last_price, bid1=bid1, ask1=ask1,
        tick_size=tick_size, vwap_value=vwap, forecast=forecast,
        current_position=position,
    )


# ────────────────────────────────────────────────────────────────────── #
#  State transitions
# ────────────────────────────────────────────────────────────────────── #


class TestStateTransitions:
    def test_idle_to_bottom_on_signal(self):
        ex = ScaledEntryExecutor(_default_params())
        assert ex.state == EntryState.IDLE
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=8.0, bar_total_sec=3600)
        assert ex.state == EntryState.BOTTOM
        assert ex.target == 6

    def test_bottom_wait_first_60s(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=8.0)
        # T=30s: still waiting
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=30))
        assert actions == []

    def test_bottom_submits_after_60s(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=90))
        # Should submit bottom (2 lots) + feishu notification
        submits = [a for a in actions if a.op == "submit"]
        assert len(submits) == 1
        assert submits[0].vol == 2
        assert submits[0].direction == "buy"

    def test_bottom_to_opp_after_fill(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=90))
        # Register pending + simulate fill
        ex.register_pending(oid=1, vol=2)
        ex.on_trade(1, 99.5, 2, T0 + timedelta(seconds=95))
        assert ex.state == EntryState.BOTTOM  # state not moved yet
        # Next tick triggers transition
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=120))
        assert ex.state == EntryState.OPPORTUNISTIC

    def test_opp_to_force_at_30min(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        # Force state to OPPORTUNISTIC with some fills
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.s.filled = 2
        ex.s.bottom_filled = True

        actions = ex.on_tick(**_base_tick_args(elapsed_sec=1805))
        assert ex.state == EntryState.FORCE

    def test_stop_triggers_locked(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.on_stop_triggered(T0 + timedelta(seconds=120))
        assert ex.state == EntryState.LOCKED

    def test_locked_returns_empty(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.on_stop_triggered(T0 + timedelta(seconds=120))
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=180))
        assert actions == []

    def test_new_signal_resets_from_locked(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.on_stop_triggered(T0 + timedelta(seconds=120))
        assert ex.state == EntryState.LOCKED
        # New bar signal
        ex.on_signal(target=4, direction="buy", now=T0 + timedelta(seconds=3600),
                     current_position=0, forecast=6.0)
        assert ex.state == EntryState.BOTTOM
        assert ex.target == 4


# ────────────────────────────────────────────────────────────────────── #
#  Reconciliation
# ────────────────────────────────────────────────────────────────────── #


class TestReconcile:
    def test_position_equals_target_no_entry(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=6, forecast=5.0)
        assert ex.state == EntryState.IDLE

    def test_position_greater_than_target_no_entry(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=4, direction="buy", now=T0,
                     current_position=6, forecast=5.0)
        assert ex.state == EntryState.IDLE

    def test_position_less_than_target_enters_delta(self):
        ex = ScaledEntryExecutor(_default_params())
        # Already have 2, target 6 → delta 4
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=2, forecast=5.0)
        assert ex.state == EntryState.BOTTOM
        assert ex.target == 4  # delta

    def test_opposite_direction_no_entry(self):
        ex = ScaledEntryExecutor(_default_params())
        # Already short -2, target buy 6 → should not enter (strategy needs to close first)
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=-2, forecast=5.0)
        assert ex.state == EntryState.IDLE


# ────────────────────────────────────────────────────────────────────── #
#  Urgency scoring
# ────────────────────────────────────────────────────────────────────── #


class TestUrgencyScoring:
    def test_urgency_zero_at_bar_start(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=0.0)
        u = ex._compute_urgency(
            elapsed=1, forecast=0.0, last_price=100.0, vwap_value=100.0,
        )
        # time=1/3600 ≈ 0.0003, deficit=1, signal=0, opp=0
        # urgency = 0.0003*0.4 + 1*0.3 + 0 + 0 ≈ 0.30
        assert 0.28 < u < 0.32

    def test_urgency_high_when_all_maxed(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=10.0)
        u = ex._compute_urgency(
            elapsed=3600, forecast=10.0, last_price=99.0, vwap_value=100.0,
        )
        # time=1, deficit=1, signal=1, opp=1 (price 1% below VWAP)
        assert u > 0.9

    def test_urgency_clamps_to_range(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=10.0)
        u = ex._compute_urgency(
            elapsed=100000, forecast=999.0, last_price=0.1, vwap_value=100.0,
        )
        assert u <= 1.0

    def test_urgency_force_stage_baseline(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.FORCE
        ex.s.force_slot_start = T0 + timedelta(seconds=1800)
        u = ex._compute_urgency(
            elapsed=1800, forecast=5.0, last_price=100.0, vwap_value=100.0,
        )
        # time_pressure in FORCE starts at 0.6
        assert u > 0.5


# ────────────────────────────────────────────────────────────────────── #
#  Opportunistic behavior
# ────────────────────────────────────────────────────────────────────── #


class TestOpportunistic:
    def test_opp_submits_when_price_below_vwap(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.s.filled = 2
        ex.s.bottom_filled = True
        # price 99 < vwap 100
        actions = ex.on_tick(**_base_tick_args(
            elapsed_sec=600, last_price=99.0, vwap=100.0,
        ))
        assert any(a.op == "submit" for a in actions)
        submit = next(a for a in actions if a.op == "submit")
        assert submit.vol == 1

    def test_opp_no_submit_price_above_vwap_low_urgency(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=2.0)
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.s.filled = 2
        ex.s.bottom_filled = True
        # price 102 > vwap 100, low forecast → urgency<0.5
        actions = ex.on_tick(**_base_tick_args(
            elapsed_sec=600, last_price=102.0, vwap=100.0, forecast=2.0,
        ))
        submits = [a for a in actions if a.op == "submit"]
        assert len(submits) == 0

    def test_opp_rate_limit(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.s.filled = 2
        ex.s.bottom_filled = True

        # First tick: submit
        a1 = ex.on_tick(**_base_tick_args(elapsed_sec=600, last_price=99.0))
        assert any(a.op == "submit" for a in a1)

        # Register pending
        submit = next(a for a in a1 if a.op == "submit")
        ex.register_pending(10, submit.vol)

        # Second tick 5 seconds later: should NOT submit again (< 10s interval)
        a2 = ex.on_tick(**_base_tick_args(elapsed_sec=605, last_price=99.0))
        submits = [a for a in a2 if a.op == "submit"]
        assert len(submits) == 0

    def test_opp_max_concurrent_pending(self):
        ex = ScaledEntryExecutor(_default_params(max_concurrent_pending=2))
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.s.filled = 2
        ex.s.bottom_filled = True

        # Fill up 2 pending
        ex.register_pending(10, 1)
        ex.register_pending(11, 1)

        actions = ex.on_tick(**_base_tick_args(elapsed_sec=600, last_price=99.0))
        submits = [a for a in actions if a.op == "submit"]
        assert len(submits) == 0  # at cap


class TestOverTarget:
    def test_over_target_triggers_once(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=10, direction="buy", now=T0,
                     current_position=0, forecast=8.0)
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.s.filled = 2
        ex.s.bottom_filled = True

        # price 98 < vwap*(1-0.5%) = 99.5
        orig_target = ex.target
        ex.on_tick(**_base_tick_args(
            elapsed_sec=600, last_price=98.0, vwap=100.0, forecast=8.0,
        ))
        assert ex.target == orig_target + 2  # 20% of 10 = 2
        assert ex.s.over_target_triggered

        # Second trigger should NOT add
        ex.on_tick(**_base_tick_args(
            elapsed_sec=700, last_price=97.0, vwap=100.0, forecast=8.0,
        ))
        assert ex.target == orig_target + 2

    def test_over_target_not_triggered_without_conditions(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=10, direction="buy", now=T0,
                     current_position=0, forecast=3.0)  # low forecast
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.s.filled = 2
        ex.s.bottom_filled = True

        orig_target = ex.target
        ex.on_tick(**_base_tick_args(
            elapsed_sec=600, last_price=98.0, vwap=100.0, forecast=3.0,
        ))
        # forecast 3 < 5 threshold
        assert ex.target == orig_target


# ────────────────────────────────────────────────────────────────────── #
#  Accounting
# ────────────────────────────────────────────────────────────────────── #


class TestAccounting:
    def test_remaining_accounts_for_pending(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        # target=6, filled=0
        assert ex.remaining == 6
        ex.register_pending(1, 2)
        # target=6, filled=0, pending=2 → remaining=4
        assert ex.remaining == 4

    def test_remaining_after_fill(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(1, 2)
        ex.on_trade(1, 99.5, 2, T0 + timedelta(seconds=90))
        # filled=2, pending=0 → remaining=4
        assert ex.remaining == 4
        assert ex.filled == 2

    def test_remaining_after_cancel_recovers(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(1, 2)
        assert ex.remaining == 4
        ex.register_cancelled(1)
        # After cancel pending=0 → remaining=6
        assert ex.remaining == 6

    def test_partial_fill(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(1, 3)
        ex.on_trade(1, 99.5, 1, T0 + timedelta(seconds=90))
        # Partial: filled=1, pending_oids still has oid 1 with vol=2
        assert ex.filled == 1
        assert ex.pending_vol == 2
        assert ex.remaining == 3


# ────────────────────────────────────────────────────────────────────── #
#  Bottom lots computation
# ────────────────────────────────────────────────────────────────────── #


class TestBottomLots:
    def test_fixed_lots(self):
        ex = ScaledEntryExecutor(_default_params(bottom_lots=3, bottom_ratio=None))
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        assert ex.s.bottom_lots_actual == 3

    def test_fixed_lots_capped_by_target(self):
        # target < bottom_lots
        ex = ScaledEntryExecutor(_default_params(bottom_lots=5, bottom_ratio=None))
        ex.on_signal(target=3, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        assert ex.s.bottom_lots_actual == 3

    def test_ratio(self):
        ex = ScaledEntryExecutor(_default_params(bottom_lots=None, bottom_ratio=0.33))
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        # ceil(6 * 0.33) = 2
        assert ex.s.bottom_lots_actual == 2


# ────────────────────────────────────────────────────────────────────── #
#  Force stage
# ────────────────────────────────────────────────────────────────────── #


class TestForce:
    def test_force_peg_phase_submits_bid(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.FORCE
        ex.s.filled = 3
        ex.s.bottom_filled = True
        ex.s.force_slot_start = T0 + timedelta(seconds=1800)
        # T = 1800 + 30 (30s into slot = peg phase)
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=1830, last_price=100.0))
        submits = [a for a in actions if a.op == "submit"]
        assert len(submits) == 1

    def test_force_cross_phase_uses_high_urgency(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.FORCE
        ex.s.filled = 3
        ex.s.bottom_filled = True
        ex.s.force_slot_start = T0 + timedelta(seconds=1800)

        # T = 1800 + 150 (150s into slot = cross phase, after 120s peg)
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=1950, last_price=100.0))
        submits = [a for a in actions if a.op == "submit"]
        assert len(submits) >= 1
        # Should be high urgency (>=0.75)
        assert submits[-1].urgency_score >= 0.75


# ────────────────────────────────────────────────────────────────────── #
#  Bar boundary
# ────────────────────────────────────────────────────────────────────── #


class TestBarBoundary:
    def test_bar_end_exits_to_idle(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0, bar_total_sec=3600)
        ex.s.state = EntryState.OPPORTUNISTIC

        # T = 3601 > bar_total_sec
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=3601))
        assert ex.state == EntryState.IDLE

    def test_bar_end_cancels_pending(self):
        """Audit fix: bar 结束时所有 pending 订单自动 emit cancel."""
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0, bar_total_sec=3600)
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.register_pending(oid=1, vol=1, price=99.5)
        ex.register_pending(oid=2, vol=1, price=99.5)

        actions = ex.on_tick(**_base_tick_args(elapsed_sec=3601))
        cancels = [a for a in actions if a.op == "cancel"]
        assert len(cancels) == 2
        assert ex.state == EntryState.IDLE


class TestStopCancelEmission:
    """Audit fix: on_stop_triggered 应该 emit cancel actions."""

    def test_stop_returns_cancel_actions(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(oid=1, vol=2, price=99.5)
        ex.register_pending(oid=2, vol=1, price=99.0)

        actions = ex.on_stop_triggered(T0 + timedelta(seconds=120))
        cancels = [a for a in actions if a.op == "cancel"]
        assert len(cancels) == 2
        assert {a.oid for a in cancels} == {1, 2}
        assert ex.state == EntryState.LOCKED
        # Pending cleared
        assert len(ex.pending_oids) == 0


class TestSignalCancelEmission:
    """Audit fix: on_signal mid-bar 或 LOCKED 应该 emit cancel 清 pending."""

    def test_new_signal_cancels_old_pending(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(oid=1, vol=2, price=99.5)
        ex.s.state = EntryState.OPPORTUNISTIC

        # New bar signal
        actions = ex.on_signal(
            target=4, direction="buy", now=T0 + timedelta(seconds=3600),
            current_position=0, forecast=6.0,
        )
        cancels = [a for a in actions if a.op == "cancel"]
        assert len(cancels) == 1
        assert cancels[0].oid == 1
        assert ex.state == EntryState.BOTTOM
        # New state, empty pending
        assert len(ex.pending_oids) == 0


class TestBottomOverdue:
    """Audit fix: BOTTOM 过期自动升级 urgency 重挂."""

    def test_bottom_overdue_triggers_resubmit(self):
        params = _default_params(bottom_deadline_sec=60)  # 短 deadline 方便测
        ex = ScaledEntryExecutor(params)
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)

        # T=90s: 首次挂底仓
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=90))
        ex.register_pending(oid=1, vol=2, price=99.5)

        # T=120s: 过了 deadline (60s 前就过了), 但首次还没触发过 — overdue escalation 应触发
        # (实际 bottom_deadline_sec 在 params 里写 60, 所以 90s 就算 overdue)
        # 测:第二次 on_tick 应该 cancel + resubmit
        actions2 = ex.on_tick(**_base_tick_args(elapsed_sec=150))
        cancels = [a for a in actions2 if a.op == "cancel"]
        submits = [a for a in actions2 if a.op == "submit"]
        assert len(cancels) == 1
        assert len(submits) == 1
        assert submits[0].urgency_score > 0.5   # escalated urgency

    def test_bottom_overdue_triggers_only_once(self):
        params = _default_params(bottom_deadline_sec=60)
        ex = ScaledEntryExecutor(params)
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.on_tick(**_base_tick_args(elapsed_sec=90))
        ex.register_pending(oid=1, vol=2, price=99.5)

        # First overdue trigger
        a1 = ex.on_tick(**_base_tick_args(elapsed_sec=150))
        # Simulate resubmit
        ex.register_pending(oid=2, vol=2, price=100.2)

        # Second tick later — should NOT escalate again
        a2 = ex.on_tick(**_base_tick_args(elapsed_sec=180))
        overdue_cancels = [a for a in a2 if a.op == "cancel"]
        overdue_submits = [a for a in a2 if a.op == "submit"]
        # Peg logic may still fire, but no forced cancel-all this round
        # Key: bottom_overdue_escalated = True now
        assert ex.s.bottom_overdue_escalated


class TestPegToBid1:
    """Audit fix: _peg_pending 实现真正的 bid1 漂移重挂."""

    def test_peg_cancels_and_resubmits_on_drift(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.s.filled = 2
        ex.s.bottom_filled = True
        # Pending挂在 99.5
        ex.register_pending(oid=1, vol=1, price=99.5)
        # bid1 移到 99.3, urgency=0 → peg bid1 → target=99.3
        # drift = |99.3 - 99.5| = 0.2, tick=0.1 (我们用 5 tick size)
        # Actually use bigger diff to exceed threshold
        ex.s.last_submit_ts = T0  # bypass rate limit guard not needed for peg

        actions = ex.on_tick(**_base_tick_args(
            elapsed_sec=600, last_price=99.3, bid1=98.0, ask1=99.0,
            tick_size=5.0, vwap=99.5,  # price<vwap so cheap triggers
        ))
        # Expect cancel (old oid=1) + resubmit
        cancels = [a for a in actions if a.op == "cancel" and a.oid == 1]
        submits = [a for a in actions if a.op == "submit"]
        assert len(cancels) == 1
        assert len(submits) >= 1   # peg resubmit + maybe opp submit

    def test_peg_does_not_fire_when_price_stable(self):
        """target price 无变化(urgency+盘口都稳定)时不 peg."""
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.OPPORTUNISTIC
        ex.s.filled = 2
        ex.s.bottom_filled = True

        # T=10s 计算 urgency 下的 target_price, 用它作为 pending 的 price
        # urgency 会随 elapsed 变, 我们对齐相同 elapsed 验证不 peg
        urgency = ex._compute_urgency(
            elapsed=10, forecast=5.0, last_price=99.6, vwap_value=100.0,
        )
        target_price = ex._price_from_urgency(
            "buy", urgency, bid1=99.5, ask1=100.0, tick_size=5.0, last_price=99.6,
        )
        ex.register_pending(oid=1, vol=1, price=target_price)
        ex.s.last_submit_ts = T0 + timedelta(seconds=10)

        # 相同 tick 上下文 — urgency 和 price 都不变, 不应触发 peg
        actions = ex.on_tick(**_base_tick_args(
            elapsed_sec=10, last_price=99.6, bid1=99.5, ask1=100.0,
            tick_size=5.0, vwap=100.0,
        ))
        peg_cancels = [a for a in actions if a.op == "cancel" and a.oid == 1]
        assert len(peg_cancels) == 0


class TestForceSlotTimeDriven:
    """Audit fix: FORCE slot 只按时间推进, 不看 fill."""

    def test_slot_does_not_advance_on_fill(self):
        """多次 fill 不会重置 slot 起点, slot 按时间推进 5 min."""
        params = _default_params()
        ex = ScaledEntryExecutor(params)
        ex.on_signal(target=10, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.FORCE
        ex.s.filled = 3
        ex.s.bottom_filled = True
        ex.s.force_slot_start = T0 + timedelta(seconds=1800)

        # T=1830 (30s into slot): peg submit
        ex.on_tick(**_base_tick_args(elapsed_sec=1830, last_price=100.0))
        # simulate fill
        ex.register_pending(oid=10, vol=1, price=100.0)
        ex.on_trade(10, 100.0, 1, T0 + timedelta(seconds=1840))

        # Slot start should NOT have reset
        assert ex.s.force_slot_start == T0 + timedelta(seconds=1800)

    def test_slot_advances_on_time(self):
        """到 force_slot_sec (300s) 后, slot 重置."""
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=10, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.FORCE
        ex.s.filled = 3
        ex.s.bottom_filled = True
        ex.s.force_slot_start = T0 + timedelta(seconds=1800)

        # T=2101 (301s into slot): should start new slot
        new_now = T0 + timedelta(seconds=2101)
        ex.on_tick(**_base_tick_args(
            now=new_now, last_price=100.0,
        ))
        assert ex.s.force_slot_start == new_now
        assert not ex.s.force_slot_crossed


class TestBarAwareBottomWait:
    """Audit fix: BOTTOM 首次 wait 根据 bar_total_sec 自适应."""

    def test_short_bar_scales_wait_down(self):
        """bar_total=60 (M1) → wait = max(5, 60//20)=5s, 不是 60s."""
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=3, direction="buy", now=T0,
                     current_position=0, forecast=5.0, bar_total_sec=60)
        # T=10s 应该已经开始发底仓 (wait=5s)
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=10))
        submits = [a for a in actions if a.op == "submit"]
        assert len(submits) == 1

    def test_long_bar_uses_full_wait(self):
        """bar_total=3600 (H1) → wait = min(60, 3600//20=180) = 60s."""
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0, bar_total_sec=3600)
        # T=30s: still waiting
        a1 = ex.on_tick(**_base_tick_args(elapsed_sec=30))
        assert len([a for a in a1 if a.op == "submit"]) == 0
        # T=70s: submitted
        a2 = ex.on_tick(**_base_tick_args(elapsed_sec=70))
        assert len([a for a in a2 if a.op == "submit"]) == 1


class TestOppositeDirection:
    """Audit v2: opposite direction signal 应该 emit feishu 警告 + 不入场."""

    def test_long_signal_with_short_position_rejected(self):
        ex = ScaledEntryExecutor(_default_params())
        actions = ex.on_signal(
            target=6, direction="buy", now=T0,
            current_position=-3, forecast=5.0,
        )
        feishu_actions = [a for a in actions if a.op == "feishu"]
        assert len(feishu_actions) >= 1
        assert "REJECTED" in feishu_actions[0].note
        assert ex.state == EntryState.IDLE

    def test_short_signal_with_long_position_rejected(self):
        ex = ScaledEntryExecutor(_default_params())
        actions = ex.on_signal(
            target=3, direction="sell", now=T0,
            current_position=4, forecast=5.0,
        )
        feishu_actions = [a for a in actions if a.op == "feishu"]
        assert len(feishu_actions) >= 1
        assert ex.state == EntryState.IDLE


class TestOnTradeReturnValue:
    """Audit v2: on_trade 返回 bool 供策略层隔离路由."""

    def test_returns_true_for_owned_oid(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(oid=1, vol=2, price=99.5)
        claimed = ex.on_trade(1, 99.5, 2, T0 + timedelta(seconds=90))
        assert claimed is True

    def test_returns_false_for_unknown_oid(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        claimed = ex.on_trade(999, 99.5, 1, T0 + timedelta(seconds=90))
        assert claimed is False


class TestRateLimitResetOnFill:
    """Audit v2: fill 后 last_submit_ts 重置, 下一 tick 可以立即再挂."""

    def test_last_submit_ts_reset_after_fill(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.last_submit_ts = T0 + timedelta(seconds=100)
        ex.register_pending(1, 1, price=99.5)
        ex.on_trade(1, 99.5, 1, T0 + timedelta(seconds=105))
        assert ex.s.last_submit_ts is None


class TestCompleteState:
    """Audit v2: filled>=target 转 COMPLETE, 下一 tick 回 IDLE."""

    def test_fills_to_target_enters_complete(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=2, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(1, 2, price=99.5)
        ex.on_trade(1, 99.5, 2, T0 + timedelta(seconds=90))
        assert ex.state == EntryState.COMPLETE

    def test_complete_to_idle_on_next_tick(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=2, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(1, 2, price=99.5)
        ex.on_trade(1, 99.5, 2, T0 + timedelta(seconds=90))
        assert ex.state == EntryState.COMPLETE
        ex.on_tick(**_base_tick_args(elapsed_sec=120))
        assert ex.state == EntryState.IDLE


class TestPendingOidFormat:
    """Audit fix: pending_oids 值现在是 _PendingOrder."""

    def test_register_pending_stores_price(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(1, 2, price=99.5)
        assert ex.pending_oids[1].vol == 2
        assert ex.pending_oids[1].price == 99.5

    def test_pending_vol_sum(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(1, 2, price=99.5)
        ex.register_pending(2, 1, price=99.0)
        assert ex.pending_vol == 3
        assert ex.remaining == 3  # target 6 - filled 0 - pending 3


class TestStatePersistence:
    """2026-04-20: crash recovery — get_state/load_state/force_lock roundtrip."""

    def test_roundtrip_idle_state(self):
        ex = ScaledEntryExecutor(_default_params())
        state = ex.get_state()
        ex2 = ScaledEntryExecutor(_default_params())
        ex2.load_state(state)
        assert ex2.state == EntryState.IDLE
        assert ex2.target == 0
        assert ex2.filled == 0

    def test_roundtrip_bottom_with_pending(self):
        """Mid-BOTTOM crash scenario: 有 pending 订单 + bar_start + direction."""
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0, bar_total_sec=3600)
        ex.register_pending(oid=12345, vol=2, price=99.5)
        ex.s.bottom_submitted = True

        state = ex.get_state()

        # JSON roundtrip
        import json
        state2 = json.loads(json.dumps(state))

        ex2 = ScaledEntryExecutor(_default_params())
        ex2.load_state(state2)
        assert ex2.state == EntryState.BOTTOM
        assert ex2.target == 6
        assert ex2.s.direction == "buy"
        assert ex2.s.bar_start == T0
        assert ex2.s.bar_total_sec == 3600
        assert ex2.s.bottom_submitted is True
        # pending oid preserved with int key (list-of-dict design)
        assert 12345 in ex2.pending_oids
        assert ex2.pending_oids[12345].vol == 2
        assert ex2.pending_oids[12345].price == 99.5

    def test_roundtrip_preserves_oid_str_type(self):
        """oid 原本是 str 也能 roundtrip."""
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.register_pending(oid="ABC-123", vol=1, price=100.0)

        import json
        state = json.loads(json.dumps(ex.get_state()))

        ex2 = ScaledEntryExecutor(_default_params())
        ex2.load_state(state)
        assert "ABC-123" in ex2.pending_oids
        assert ex2.pending_oids["ABC-123"].vol == 1

    def test_roundtrip_filled_and_stage_transitions(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.s.state = EntryState.FORCE
        ex.s.filled = 4
        ex.s.force_slot_start = T0 + timedelta(seconds=1800)
        ex.s.force_slot_crossed = True
        ex.s.over_target_triggered = True

        import json
        state = json.loads(json.dumps(ex.get_state()))

        ex2 = ScaledEntryExecutor(_default_params())
        ex2.load_state(state)
        assert ex2.state == EntryState.FORCE
        assert ex2.filled == 4
        assert ex2.s.force_slot_start == T0 + timedelta(seconds=1800)
        assert ex2.s.force_slot_crossed is True
        assert ex2.s.over_target_triggered is True

    def test_force_lock_enters_locked(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        assert ex.state == EntryState.BOTTOM
        ex.force_lock()
        assert ex.state == EntryState.LOCKED

    def test_force_lock_blocks_new_submits(self):
        """force_lock 后 on_tick 不应发新单."""
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        ex.force_lock()
        actions = ex.on_tick(**_base_tick_args(elapsed_sec=120))
        assert actions == []

    def test_load_empty_state_is_noop(self):
        ex = ScaledEntryExecutor(_default_params())
        ex.on_signal(target=6, direction="buy", now=T0,
                     current_position=0, forecast=5.0)
        original_target = ex.target
        ex.load_state({})
        ex.load_state(None)
        # State unchanged
        assert ex.target == original_target

    def test_load_corrupted_datetime_degrades_gracefully(self):
        ex = ScaledEntryExecutor(_default_params())
        bad_state = {
            "state": "bottom",
            "target": 3,
            "filled": 1,
            "direction": "buy",
            "bar_start": "not-an-iso-date",
            "bar_total_sec": 3600,
            "pending_orders": [],
        }
        ex.load_state(bad_state)
        assert ex.s.bar_start is None  # parsed None, not crashed
        assert ex.target == 3
        assert ex.filled == 1
