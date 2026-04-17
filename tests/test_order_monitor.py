"""Unit tests for modules.order_monitor.OrderMonitor (incl. escalation)."""
from __future__ import annotations

import time

from modules.order_monitor import OrderMonitor, ESCALATION_SCHEDULE


class TestBasicTracking:
    def test_on_send_registers(self):
        om = OrderMonitor()
        om.on_send(oid=1, vol=3, px=100.0, urgency="normal")
        assert om.pending_count() == 1
        info = om.get_order(1)
        assert info["vol"] == 3
        assert info["urgency"] == "normal"
        assert info["escalations"] == 0

    def test_on_fill_removes(self):
        om = OrderMonitor()
        om.on_send(oid=1, vol=3, px=100.0)
        om.on_fill(1)
        assert om.pending_count() == 0

    def test_on_cancel_removes(self):
        om = OrderMonitor()
        om.on_send(oid=1, vol=3, px=100.0)
        om.on_cancel(1)
        assert om.pending_count() == 0

    def test_unknown_urgency_fallback_to_normal(self):
        om = OrderMonitor()
        om.on_send(oid=1, vol=1, px=100.0, urgency="bogus")
        assert om.get_order(1)["urgency"] == "normal"

    def test_oid_none_ignored(self):
        om = OrderMonitor()
        om.on_send(None, vol=1, px=100.0)
        assert om.pending_count() == 0


class TestLegacyTimeout:
    def test_expired_cancelled(self):
        om = OrderMonitor(timeout=0.1)
        om.on_send(oid=1, vol=1, px=100.0)
        time.sleep(0.15)
        cancelled = []
        om.check_timeouts(lambda oid: cancelled.append(oid))
        assert cancelled == [1]
        assert om.pending_count() == 0

    def test_not_expired_remains(self):
        om = OrderMonitor(timeout=60)
        om.on_send(oid=1, vol=1, px=100.0)
        cancelled = []
        om.check_timeouts(lambda oid: cancelled.append(oid))
        assert cancelled == []
        assert om.pending_count() == 1


class TestEscalation:
    def test_no_escalation_before_threshold(self):
        om = OrderMonitor()
        om.on_send(oid=1, vol=1, px=100.0, urgency="normal")
        # normal schedule: (3s, cross) — we are at age 0
        out = om.check_escalation(now_ts=time.time())
        assert out == []

    def test_escalates_at_threshold(self):
        om = OrderMonitor()
        # Inject fake send time
        om.on_send(oid=1, vol=1, px=100.0, urgency="normal")
        om.get_order(1)["t"] = time.time() - 3.5  # 3.5s ago
        out = om.check_escalation()
        assert len(out) == 1
        oid, next_urgency, _ = out[0]
        assert oid == 1
        assert next_urgency == "cross"
        # escalations count incremented so next check doesn't re-emit
        assert om.get_order(1)["escalations"] == 1

    def test_second_escalation_at_next_threshold(self):
        om = OrderMonitor()
        om.on_send(oid=1, vol=1, px=100.0, urgency="normal")
        info = om.get_order(1)
        info["t"] = time.time() - 10.0   # 10s ago, both thresholds 3s+8s passed

        # First call emits one escalation (to cross at 3s)
        out1 = om.check_escalation()
        assert len(out1) == 1
        assert out1[0][1] == "cross"

        # Second call emits next escalation (to urgent at 8s)
        out2 = om.check_escalation()
        assert len(out2) == 1
        assert out2[0][1] == "urgent"

        # Third call: next step is critical at 15s, we are at 10s — not yet
        out3 = om.check_escalation()
        assert out3 == []

    def test_critical_never_escalates(self):
        om = OrderMonitor()
        om.on_send(oid=1, vol=1, px=100.0, urgency="critical")
        om.get_order(1)["t"] = time.time() - 30.0
        out = om.check_escalation()
        assert out == []

    def test_passive_chain(self):
        om = OrderMonitor()
        om.on_send(oid=1, vol=1, px=100.0, urgency="passive")
        info = om.get_order(1)
        info["t"] = time.time() - 20.0  # all thresholds 5/10/15 passed

        levels = []
        for _ in range(5):
            out = om.check_escalation()
            if out:
                levels.append(out[0][1])
            else:
                break
        assert levels == ["normal", "cross", "urgent"]

    def test_fill_during_escalation_removes(self):
        om = OrderMonitor()
        om.on_send(oid=1, vol=1, px=100.0, urgency="normal")
        om.get_order(1)["t"] = time.time() - 3.5
        out = om.check_escalation()
        assert len(out) == 1
        om.on_fill(1)
        out2 = om.check_escalation()
        assert out2 == []


class TestScheduleSanity:
    def test_all_urgency_levels_have_schedule_entry(self):
        required = {"passive", "normal", "cross", "urgent", "critical"}
        assert set(ESCALATION_SCHEDULE.keys()) == required

    def test_thresholds_monotonic_per_level(self):
        for urgency, schedule in ESCALATION_SCHEDULE.items():
            times = [t for t, _ in schedule]
            assert times == sorted(times), f"{urgency} schedule not monotonic"

    def test_terminal_level_is_critical(self):
        assert ESCALATION_SCHEDULE["critical"] == []
