"""Unit tests for modules.risk.RiskManager — tick/minute stop loss layer.

Covers:
- tick-level peak/trough tracking (long + short)
- tick-level hard stop (immediate, no confirmation)
- 1-minute-gated trail stop (single check per minute boundary)
- direction flip & flat reset
- back-compat with legacy check()
- state serialization round-trip
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from modules.risk import RiskManager


# ---------------------------------------------------------------------------
# peak / trough tick tracking
# ---------------------------------------------------------------------------

class TestPeakTroughTick:
    def test_long_peak_rises_with_price(self):
        rm = RiskManager(capital=1_000_000)
        rm.update_peak_trough_tick(100.0, net_pos=1)
        rm.update_peak_trough_tick(101.0, net_pos=1)
        rm.update_peak_trough_tick(100.5, net_pos=1)
        assert rm.peak_price == 101.0
        assert rm.trough_price == 0.0

    def test_short_trough_falls_with_price(self):
        rm = RiskManager(capital=1_000_000)
        rm.update_peak_trough_tick(100.0, net_pos=-1)
        rm.update_peak_trough_tick(99.0, net_pos=-1)
        rm.update_peak_trough_tick(99.5, net_pos=-1)
        assert rm.trough_price == 99.0
        assert rm.peak_price == 0.0

    def test_flat_resets_both(self):
        rm = RiskManager(capital=1_000_000)
        rm.update_peak_trough_tick(100.0, net_pos=1)
        rm.update_peak_trough_tick(105.0, net_pos=1)
        rm.update_peak_trough_tick(104.0, net_pos=0)
        assert rm.peak_price == 0.0
        assert rm.trough_price == 0.0

    def test_direction_flip_resets_extremes(self):
        """若策略未先平仓直接反向(不常见但健壮性要求),重置极值."""
        rm = RiskManager(capital=1_000_000)
        rm.update_peak_trough_tick(100.0, net_pos=1)
        rm.update_peak_trough_tick(105.0, net_pos=1)
        # flip to short at 104
        rm.update_peak_trough_tick(104.0, net_pos=-1)
        assert rm.peak_price == 0.0
        assert rm.trough_price == 104.0

    def test_peak_monotonic_on_dip(self):
        rm = RiskManager(capital=1_000_000)
        rm.update_peak_trough_tick(100.0, net_pos=2)
        rm.update_peak_trough_tick(110.0, net_pos=2)
        rm.update_peak_trough_tick(105.0, net_pos=2)  # dip
        assert rm.peak_price == 110.0


# ---------------------------------------------------------------------------
# tick-level hard stop
# ---------------------------------------------------------------------------

class TestHardStopTick:
    def test_long_triggers_at_boundary(self):
        rm = RiskManager()
        # avg=100, hard=0.5% → line=99.5
        action, reason = rm.check_hard_stop_tick(
            price=99.5, avg_price=100.0, net_pos=1, hard_stop_pct=0.5
        )
        assert action == "HARD_STOP"
        assert "99.5" in reason

    def test_long_no_trigger_above_line(self):
        rm = RiskManager()
        action, _ = rm.check_hard_stop_tick(
            price=99.6, avg_price=100.0, net_pos=1, hard_stop_pct=0.5
        )
        assert action is None

    def test_short_triggers_above_line(self):
        rm = RiskManager()
        # avg=100, hard=0.5% → line=100.5
        action, reason = rm.check_hard_stop_tick(
            price=100.5, avg_price=100.0, net_pos=-1, hard_stop_pct=0.5
        )
        assert action == "HARD_STOP"
        assert "100.5" in reason

    def test_short_no_trigger_below_line(self):
        rm = RiskManager()
        action, _ = rm.check_hard_stop_tick(
            price=100.4, avg_price=100.0, net_pos=-1, hard_stop_pct=0.5
        )
        assert action is None

    def test_flat_never_triggers(self):
        rm = RiskManager()
        action, _ = rm.check_hard_stop_tick(
            price=50.0, avg_price=100.0, net_pos=0, hard_stop_pct=0.5
        )
        assert action is None

    def test_zero_avg_price_guarded(self):
        rm = RiskManager()
        action, _ = rm.check_hard_stop_tick(
            price=50.0, avg_price=0.0, net_pos=1, hard_stop_pct=0.5
        )
        assert action is None


# ---------------------------------------------------------------------------
# 1-minute gated trail stop
# ---------------------------------------------------------------------------

class TestTrailMinutely:
    def _setup_long(self, peak: float = 100.0) -> RiskManager:
        rm = RiskManager()
        rm.update_peak_trough_tick(peak, net_pos=1)
        return rm

    def _setup_short(self, trough: float = 100.0) -> RiskManager:
        rm = RiskManager()
        rm.update_peak_trough_tick(trough, net_pos=-1)
        return rm

    def test_long_trail_triggers_on_minute_boundary(self):
        rm = self._setup_long(peak=100.0)
        # trailing=0.3% → line=99.7
        t0 = datetime(2026, 4, 17, 10, 0, 0)
        action, reason = rm.check_trail_minutely(
            price=99.7, now=t0, net_pos=1, trailing_pct=0.3,
        )
        assert action == "TRAIL_STOP"
        assert "99.7" in reason

    def test_long_trail_no_trigger_above_line(self):
        rm = self._setup_long(peak=100.0)
        t0 = datetime(2026, 4, 17, 10, 0, 0)
        action, _ = rm.check_trail_minutely(
            price=99.8, now=t0, net_pos=1, trailing_pct=0.3,
        )
        assert action is None

    def test_short_trail_triggers_on_minute_boundary(self):
        rm = self._setup_short(trough=100.0)
        # trailing=0.3% → line=100.3
        t0 = datetime(2026, 4, 17, 10, 0, 0)
        action, reason = rm.check_trail_minutely(
            price=100.3, now=t0, net_pos=-1, trailing_pct=0.3,
        )
        assert action == "TRAIL_STOP"

    def test_skips_same_minute_after_first_check(self):
        """Critical: each minute judges exactly once, not per tick."""
        rm = self._setup_long(peak=100.0)
        t0 = datetime(2026, 4, 17, 10, 0, 5)
        # First call within minute: judges (price above line → no trigger).
        action1, _ = rm.check_trail_minutely(
            price=99.8, now=t0, net_pos=1, trailing_pct=0.3,
        )
        assert action1 is None
        # Next tick in SAME minute with price below line: must NOT re-judge.
        action2, _ = rm.check_trail_minutely(
            price=99.5, now=t0 + timedelta(seconds=30),
            net_pos=1, trailing_pct=0.3,
        )
        assert action2 is None

    def test_judges_again_on_next_minute(self):
        rm = self._setup_long(peak=100.0)
        t0 = datetime(2026, 4, 17, 10, 0, 5)
        rm.check_trail_minutely(
            price=99.8, now=t0, net_pos=1, trailing_pct=0.3,
        )
        t1 = datetime(2026, 4, 17, 10, 1, 5)
        action, _ = rm.check_trail_minutely(
            price=99.5, now=t1, net_pos=1, trailing_pct=0.3,
        )
        assert action == "TRAIL_STOP"

    def test_flat_position_resets_minute_gate(self):
        rm = self._setup_long(peak=100.0)
        t0 = datetime(2026, 4, 17, 10, 0, 0)
        rm.check_trail_minutely(99.9, t0, net_pos=1, trailing_pct=0.3)
        # Position closed and reopened same minute:
        rm.update_peak_trough_tick(200.0, net_pos=0)
        rm.update_peak_trough_tick(200.0, net_pos=1)
        action, _ = rm.check_trail_minutely(
            price=199.0, now=t0, net_pos=1, trailing_pct=0.3,
        )
        # Fresh position, line = 200 * 0.997 = 199.4 → 199 <= 199.4, should trigger
        assert action == "TRAIL_STOP"

    def test_al_10am_incident_replay(self):
        """Replay Simon's 2026-04-17 10:00 AL case.

        V8: peak=25520 (assumed), trailing_pct=0.3 → line≈25443.4.
        At 09:xx price dropped through line but old system waited until
        10:00 H1 bar close to trigger. New system fires at 09:xx+1 minute.
        """
        rm = RiskManager()
        # Accumulate peak from ticks during 09:00 hour.
        for p in [25500.0, 25520.0, 25515.0]:
            rm.update_peak_trough_tick(p, net_pos=4)
        assert rm.peak_price == 25520.0
        # At 09:15 price dips to 25440 (below line 25443.36).
        t = datetime(2026, 4, 17, 9, 15, 0)
        action, reason = rm.check_trail_minutely(
            price=25440.0, now=t, net_pos=4, trailing_pct=0.3,
        )
        assert action == "TRAIL_STOP"
        # 应该在 09:15 就触发,而非等到 10:00


# ---------------------------------------------------------------------------
# backward compat: existing check()
# ---------------------------------------------------------------------------

class TestLegacyCheck:
    def test_legacy_check_still_works_for_long(self):
        rm = RiskManager(capital=1_000_000)
        rm.update(equity=1_000_000)
        action, _ = rm.check(
            close=99.5, avg_price=100.0, peak_price=100.0,
            pos_profit=-500, net_pos=1,
            hard_stop_pct=0.5, trailing_pct=0.3, equity_stop_pct=2.0,
        )
        assert action == "HARD_STOP"

    def test_legacy_check_flat_returns_none(self):
        rm = RiskManager()
        action, _ = rm.check(
            close=99.5, avg_price=100.0, peak_price=100.0,
            pos_profit=0, net_pos=0,
        )
        assert action is None


# ---------------------------------------------------------------------------
# state persistence
# ---------------------------------------------------------------------------

class TestStateRoundTrip:
    def test_save_load_preserves_price_extremes(self):
        rm = RiskManager(capital=500_000)
        rm.update(equity=510_000)
        rm.update_peak_trough_tick(105.0, net_pos=1)
        state = rm.get_state()

        rm2 = RiskManager(capital=500_000)
        rm2.load_state(state)
        assert rm2.peak_equity == 510_000
        assert rm2.peak_price == 105.0

    def test_save_load_preserves_trough(self):
        rm = RiskManager()
        rm.update_peak_trough_tick(95.0, net_pos=-2)
        state = rm.get_state()

        rm2 = RiskManager()
        rm2.load_state(state)
        assert rm2.trough_price == 95.0


# ---------------------------------------------------------------------------
# priority: hard stop must outrank trail stop when both could fire
# ---------------------------------------------------------------------------

class TestPriority:
    def test_tick_hard_stop_fires_even_if_trail_would_also_fire(self):
        """Hard stop is tick-level, trail stop is minute-level.

        Strategy contract: call hard stop check first on every tick.
        Trail stop check is a separate call gated by minute boundary.
        """
        rm = RiskManager()
        rm.update_peak_trough_tick(100.0, net_pos=1)
        # Avg=100, close=99.5 → both hard (<=99.5) and trail (<=99.7) would fire
        action, _ = rm.check_hard_stop_tick(
            price=99.5, avg_price=100.0, net_pos=1, hard_stop_pct=0.5,
        )
        assert action == "HARD_STOP"
