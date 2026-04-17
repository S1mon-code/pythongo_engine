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
        # Should submit bottom (2 lots)
        assert len(actions) == 1
        assert actions[0].op == "submit"
        assert actions[0].vol == 2
        assert actions[0].direction == "buy"

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
        assert actions == []
        assert ex.state == EntryState.IDLE
