"""Unit tests for the merged alarm board."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from cranefleet.alarms import (
    AlarmBoard,
    Incident,
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    STATUS_ACKNOWLEDGED,
    STATUS_ACTIVE,
    STATUS_RESOLVED,
)
from cranefleet.errors import AlarmNotFound, AlarmStateError


class FakeClock:
    """A controllable UTC clock for escalation tests."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def board(clock: FakeClock, deadlines: dict[str, float] | None = None) -> AlarmBoard:
    return AlarmBoard(
        escalation_seconds=deadlines or {SEVERITY_MINOR: 60.0, SEVERITY_MAJOR: 120.0},
        clock=clock,
    )


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.board = board(self.clock)

    def test_repeated_alarms_at_one_location_merge(self) -> None:
        first = self.board.report("crane-1/slew", "slew-fault", SEVERITY_MAJOR, "回转故障")
        self.clock.advance(5)
        second = self.board.report("crane-1/slew", "slew-fault", SEVERITY_MAJOR, "回转故障")
        third = self.board.report("crane-1/slew", "slew-fault", SEVERITY_MAJOR, "回转故障")
        self.assertEqual(first.incident_id, second.incident_id)
        self.assertEqual(third.occurrences, 3)
        self.assertEqual(self.board.open_count(), 1)
        self.assertEqual(third.first_seen, first.first_seen)
        self.assertNotEqual(third.last_seen, first.last_seen)

    def test_different_locations_stay_separate(self) -> None:
        a = self.board.report("crane-1/slew", "slew-fault")
        b = self.board.report("crane-2/slew", "slew-fault")
        self.assertNotEqual(a.incident_id, b.incident_id)
        self.assertEqual(self.board.open_count(), 2)

    def test_cascade_kinds_collect_on_one_incident(self) -> None:
        incident = self.board.report("crane-1/limit/slew", "limit-slew", SEVERITY_MINOR)
        self.board.report("crane-1/limit/slew", "limit-slew", SEVERITY_MINOR)
        merged = self.board.get(incident.incident_id)
        self.assertEqual(merged.kinds, ["limit-slew"])
        self.assertEqual(merged.occurrences, 2)

    def test_higher_incoming_severity_raises_incident(self) -> None:
        self.board.report("site/wind", "wind", SEVERITY_MINOR)
        incident = self.board.report("site/wind", "wind", SEVERITY_CRITICAL)
        self.assertEqual(incident.severity, SEVERITY_CRITICAL)
        self.assertEqual(self.board.open_count(), 1)

    def test_blank_location_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.board.report("  ", "wind")


class EscalationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.board = board(self.clock)

    def test_unacknowledged_incident_escalates_after_timeout(self) -> None:
        incident = self.board.report("crane-1/limit/trolley", "limit-trolley", SEVERITY_MINOR)
        self.assertEqual(incident.severity, SEVERITY_MINOR)
        self.clock.advance(59)
        self.assertEqual(self.board.list()[0].severity, SEVERITY_MINOR)
        self.clock.advance(2)
        escalated = self.board.list()[0]
        self.assertEqual(escalated.severity, SEVERITY_MAJOR)
        self.assertIsNotNone(escalated.escalated_at)

    def test_escalation_walks_to_critical(self) -> None:
        self.board.report("crane-1/limit/trolley", "limit-trolley", SEVERITY_MINOR)
        self.clock.advance(61)
        self.board.escalate_due()
        self.clock.advance(121)
        moved = self.board.escalate_due()
        self.assertEqual(len(moved), 1)
        self.assertEqual(self.board.list()[0].severity, SEVERITY_CRITICAL)
        # Critical has no further level; extra time changes nothing.
        self.clock.advance(10_000)
        self.assertEqual(self.board.escalate_due(), [])
        self.assertEqual(self.board.list()[0].severity, SEVERITY_CRITICAL)

    def test_acknowledged_incident_never_escalates(self) -> None:
        incident = self.board.report("crane-1/slew", "slew-fault", SEVERITY_MINOR)
        self.board.acknowledge(incident.incident_id, "zhang")
        self.clock.advance(10_000)
        stale = self.board.list()[0]
        self.assertEqual(stale.severity, SEVERITY_MINOR)
        self.assertEqual(stale.status, STATUS_ACKNOWLEDGED)

    def test_repeated_reports_do_not_reset_escalation_clock(self) -> None:
        self.board.report("crane-1/slew", "slew-fault", SEVERITY_MINOR)
        self.clock.advance(40)
        self.board.report("crane-1/slew", "slew-fault", SEVERITY_MINOR)
        self.clock.advance(25)
        # Timer started at first_seen (65s ago), not at the second report.
        self.assertEqual(self.board.list()[0].severity, SEVERITY_MAJOR)


class AcknowledgementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.board = board(self.clock)

    def test_new_report_after_ack_opens_follow_up_incident(self) -> None:
        first = self.board.report("crane-1/slew", "slew-fault", SEVERITY_MAJOR)
        acknowledged = self.board.acknowledge(first.incident_id, "zhang")
        self.assertEqual(acknowledged.status, STATUS_ACKNOWLEDGED)
        self.assertEqual(acknowledged.occurrences, 1)
        follow_up = self.board.report("crane-1/slew", "slew-fault", SEVERITY_MAJOR)
        self.assertNotEqual(follow_up.incident_id, first.incident_id)
        self.assertEqual(follow_up.follows, first.incident_id)
        self.assertEqual(follow_up.occurrences, 1)
        # The acknowledged record keeps its state and counter.
        original = self.board.get(first.incident_id)
        self.assertEqual(original.status, STATUS_ACKNOWLEDGED)
        self.assertEqual(original.occurrences, 1)
        self.assertEqual(original.acknowledged_by, "zhang")

    def test_cannot_acknowledge_twice_or_resolve_twice(self) -> None:
        incident = self.board.report("crane-1/slew", "slew-fault")
        self.board.acknowledge(incident.incident_id, "zhang")
        with self.assertRaises(AlarmStateError):
            self.board.acknowledge(incident.incident_id, "li")
        self.board.resolve(incident.incident_id, "zhang")
        with self.assertRaises(AlarmStateError):
            self.board.resolve(incident.incident_id, "zhang")

    def test_resolved_incident_is_never_reopened(self) -> None:
        first = self.board.report("site/wind", "wind", SEVERITY_MAJOR)
        self.board.resolve(first.incident_id, "zhang")
        second = self.board.report("site/wind", "wind", SEVERITY_MAJOR)
        self.assertNotEqual(first.incident_id, second.incident_id)
        self.assertEqual(second.follows, "")
        history = self.board.list()
        self.assertEqual({item.status for item in history}, {STATUS_ACTIVE, STATUS_RESOLVED})

    def test_unknown_id_raises_not_found(self) -> None:
        with self.assertRaises(AlarmNotFound):
            self.board.acknowledge("missing", "zhang")
        with self.assertRaises(AlarmNotFound):
            self.board.resolve("missing", "zhang")


class ResolveBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.board = board(self.clock)

    def test_resolve_batch_clears_whole_location(self) -> None:
        wind = self.board.report("site/wind", "wind", SEVERITY_MAJOR)
        slew = self.board.report("crane-1/slew", "slew-fault", SEVERITY_MAJOR)
        other = self.board.report("crane-2/slew", "slew-fault", SEVERITY_MAJOR)
        closed = self.board.resolve_batch("zhang", location="crane-1")
        self.assertEqual([item.incident_id for item in closed], [slew.incident_id])
        self.board.resolve_batch("zhang", kind="wind")
        statuses = {item.incident_id: item.status for item in self.board.list()}
        self.assertEqual(statuses[wind.incident_id], STATUS_RESOLVED)
        self.assertEqual(statuses[slew.incident_id], STATUS_RESOLVED)
        self.assertEqual(statuses[other.incident_id], STATUS_ACTIVE)

    def test_batch_closes_acknowledged_and_active_together(self) -> None:
        first = self.board.report("crane-1/slew", "slew-fault")
        self.board.acknowledge(first.incident_id, "zhang")
        second = self.board.report("crane-1/slew", "slew-fault")
        closed = self.board.resolve_batch("zhang", location="crane-1")
        self.assertEqual({item.incident_id for item in closed}, {first.incident_id, second.incident_id})
        self.assertEqual(self.board.open_count(), 0)

    def test_auto_resolve_only_touches_matching_kind(self) -> None:
        wind = self.board.report("site/wind", "wind", SEVERITY_MAJOR)
        limit = self.board.report("site/limit/slew", "limit-slew", SEVERITY_MINOR)
        closed = self.board.auto_resolve("site/wind", "wind")
        self.assertEqual([item.incident_id for item in closed], [wind.incident_id])
        self.assertEqual(self.board.get(limit.incident_id).status, STATUS_ACTIVE)
        self.assertEqual(self.board.get(wind.incident_id).status, STATUS_RESOLVED)


class QueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.board = board(self.clock)

    def test_list_newest_first_with_filters(self) -> None:
        old = self.board.report("a", "wind")
        self.clock.advance(1)
        new = self.board.report("b", "slew-fault")
        listing = self.board.list()
        self.assertEqual([item.incident_id for item in listing], [new.incident_id, old.incident_id])
        self.assertEqual([item.incident_id for item in self.board.list(location="a")], [old.incident_id])
        self.assertEqual([item.incident_id for item in self.board.list(kind="wind")], [old.incident_id])
        self.assertEqual(len(self.board.list(limit=1)), 1)

    def test_counts_by_severity_only_open(self) -> None:
        minor = self.board.report("a", "limit-slew", SEVERITY_MINOR)
        self.board.report("b", "wind", SEVERITY_CRITICAL)
        self.board.resolve(minor.incident_id, "zhang")
        counts = self.board.counts_by_severity()
        self.assertEqual(counts[SEVERITY_MINOR], 0)
        self.assertEqual(counts[SEVERITY_CRITICAL], 1)

    def test_capacity_prunes_resolved_history_but_keeps_open(self) -> None:
        small = AlarmBoard(capacity=3, clock=self.clock)
        keep = small.report("open", "wind", SEVERITY_MAJOR)
        for index in range(5):
            incident = small.report(f"loc-{index}", "wind", SEVERITY_MAJOR)
            small.resolve(incident.incident_id, "zhang")
        ids = {item.incident_id for item in small.snapshot()}
        self.assertIn(keep.incident_id, ids)
        self.assertLessEqual(len(ids), 3)

    def test_serialisation_round_trip(self) -> None:
        first = self.board.report("crane-1/slew", "slew-fault", SEVERITY_MAJOR, title="回转故障")
        self.board.acknowledge(first.incident_id, "zhang")
        follow_up = self.board.report("crane-1/slew", "slew-fault", SEVERITY_MAJOR)
        restored_incidents = [
            Incident.from_dict(item.to_dict()) for item in self.board.snapshot()
        ]
        clone = board(self.clock)
        clone.restore(restored_incidents)
        self.assertEqual(clone.get(first.incident_id).status, STATUS_ACKNOWLEDGED)
        self.assertEqual(clone.get(follow_up.incident_id).follows, first.incident_id)
        self.assertEqual(clone.open_count(), 2)


if __name__ == "__main__":
    unittest.main()
