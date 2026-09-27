"""Unit tests for the site alarm board: merging, escalation and lifecycle."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from cranefleet import alerts as alerts_module
from cranefleet.alerts import (
    LEVEL_CRITICAL,
    LEVEL_MAJOR,
    LEVEL_MINOR,
    STATUS_ACKNOWLEDGED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    AlarmBoard,
)
from cranefleet.audit import Audit
from cranefleet.store import MemoryStore


class FakeClock:
    """Controllable clock for escalation tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 26, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def board_with_timeout(timeouts: dict[str, float] | None = None) -> tuple[AlarmBoard, FakeClock]:
    clock = FakeClock()
    return AlarmBoard(timeouts=timeouts, clock=clock), clock


class ReportMergeTests(unittest.TestCase):
    def test_reports_for_same_location_merge_into_one(self) -> None:
        board = AlarmBoard()
        first = board.report("crane-1/slew-drive", LEVEL_MINOR, "首次故障")
        second = board.report("crane-1/slew-drive", LEVEL_MINOR, "连锁报警")
        third = board.report("crane-1/slew-drive", LEVEL_MINOR, "又一条连锁")
        self.assertTrue(first.created)
        self.assertEqual(second.alarm.alarm_id, first.alarm.alarm_id)
        self.assertTrue(second.merged)
        self.assertEqual(third.alarm.repeat_count(), 3)
        self.assertEqual(third.alarm.message, "又一条连锁")
        self.assertEqual(len(board.list_open()), 1)

    def test_different_locations_remain_separate(self) -> None:
        board = AlarmBoard()
        board.report("crane-1/slew-drive", LEVEL_MINOR, "a")
        board.report("crane-2/slew-drive", LEVEL_MINOR, "b")
        self.assertEqual(len(board.list_open()), 2)

    def test_more_severe_report_raises_level(self) -> None:
        board = AlarmBoard()
        board.report("zone-1", LEVEL_MINOR, "轻微")
        outcome = board.report("zone-1", LEVEL_MAJOR, "恶化")
        self.assertTrue(outcome.raised)
        self.assertEqual(outcome.alarm.severity, LEVEL_MAJOR)
        # A lower-severity repeat never downgrades.
        outcome = board.report("zone-1", LEVEL_MINOR, "抖动")
        self.assertFalse(outcome.raised)
        self.assertEqual(outcome.alarm.severity, LEVEL_MAJOR)

    def test_empty_location_rejected(self) -> None:
        board = AlarmBoard()
        with self.assertRaises(ValueError):
            board.report("  ", LEVEL_MINOR, "x")

    def test_unknown_severity_rejected(self) -> None:
        board = AlarmBoard()
        with self.assertRaises(ValueError):
            board.report("loc", "fatal", "x")


class EscalationTests(unittest.TestCase):
    def test_unacknowledged_alarm_escalates_after_timeout(self) -> None:
        board, clock = board_with_timeout({LEVEL_MINOR: 60.0, LEVEL_MAJOR: 60.0})
        outcome = board.report("crane-1/hoist", LEVEL_MINOR, "过热")
        self.assertEqual(outcome.alarm.severity, LEVEL_MINOR)
        clock.advance(30)
        board.escalate_due()
        self.assertEqual(board.get(outcome.alarm.alarm_id).severity, LEVEL_MINOR)
        clock.advance(31)
        escalated = board.escalate_due()
        self.assertEqual(len(escalated), 1)
        self.assertEqual(escalated[0].severity, LEVEL_MAJOR)
        self.assertEqual(escalated[0].escalations, 1)
        clock.advance(61)
        board.escalate_due()
        self.assertEqual(board.get(outcome.alarm.alarm_id).severity, LEVEL_CRITICAL)
        self.assertEqual(board.get(outcome.alarm.alarm_id).escalations, 2)

    def test_escalation_is_evaluated_lazily_on_report_and_list(self) -> None:
        board, clock = board_with_timeout({LEVEL_MINOR: 10.0, LEVEL_MAJOR: 10.0})
        original = board.report("loc/a", LEVEL_MINOR, "a")
        board.report("loc/b", LEVEL_MINOR, "b")
        clock.advance(11)
        # Touching the board with another report escalates due alarms too.
        board.report("loc/c", LEVEL_MINOR, "c")
        self.assertEqual(board.get(original.alarm.alarm_id).severity, LEVEL_MAJOR)
        counts = board.counts()["by_severity"]
        self.assertEqual(counts[LEVEL_MAJOR], 2)
        self.assertEqual(counts[LEVEL_MINOR], 1)

    def test_critical_never_escalates_further(self) -> None:
        board, clock = board_with_timeout({LEVEL_CRITICAL: 1.0})
        board.report("loc", LEVEL_CRITICAL, "顶格")
        clock.advance(100)
        self.assertEqual(board.escalate_due(), [])


class AcknowledgementTests(unittest.TestCase):
    def test_acknowledgement_stops_escalation(self) -> None:
        board, clock = board_with_timeout({LEVEL_MINOR: 60.0, LEVEL_MAJOR: 60.0})
        alarm = board.report("loc", LEVEL_MINOR, "x").alarm
        board.acknowledge(alarm.alarm_id, operator="lao-wang")
        clock.advance(600)
        self.assertEqual(board.escalate_due(), [])
        updated = board.get(alarm.alarm_id)
        self.assertEqual(updated.status, STATUS_ACKNOWLEDGED)
        self.assertEqual(updated.acknowledged_by, "lao-wang")
        self.assertEqual(updated.severity, LEVEL_MINOR)

    def test_new_report_does_not_overwrite_acknowledged_alarm(self) -> None:
        board = AlarmBoard()
        original = board.report("crane-1/slew-drive", LEVEL_MINOR, "原始报警").alarm
        board.acknowledge(original.alarm_id, operator="lao-wang")
        outcome = board.report("crane-1/slew-drive", LEVEL_MAJOR, "确认后新报警")
        self.assertTrue(outcome.followup)
        self.assertNotEqual(outcome.alarm.alarm_id, original.alarm_id)
        self.assertEqual(outcome.alarm.root_id, original.alarm_id)
        # The acknowledged record keeps its original message and severity.
        owned = board.get(original.alarm_id)
        self.assertEqual(owned.message, "原始报警")
        self.assertEqual(owned.severity, LEVEL_MINOR)
        # The follow-up links back and shows up as a second open row.
        self.assertEqual(owned.followup_id, outcome.alarm.alarm_id)
        self.assertEqual(len(board.list_open()), 2)

    def test_resolve_or_ack_missing_alarm(self) -> None:
        board = AlarmBoard()
        with self.assertRaises(alerts_module.AlertNotFound):
            board.acknowledge("nope")
        with self.assertRaises(alerts_module.AlertNotFound):
            board.resolve("nope")


class ResolutionTests(unittest.TestCase):
    def test_resolve_clears_followup_chain_in_one_call(self) -> None:
        board = AlarmBoard()
        root = board.report("loc", LEVEL_MINOR, "first").alarm
        board.acknowledge(root.alarm_id)
        followup = board.report("loc", LEVEL_MAJOR, "second").alarm
        cleared = board.resolve(followup.alarm_id, operator="lao-wang")
        ids = {alarm.alarm_id for alarm in cleared}
        self.assertEqual(ids, {root.alarm_id, followup.alarm_id})
        self.assertTrue(all(alarm.status == STATUS_RESOLVED for alarm in cleared))
        self.assertEqual(board.list_open(), [])
        self.assertIsNone(board.open_for("loc"))

    def test_resolving_acknowledged_root_also_clears_followups(self) -> None:
        board = AlarmBoard()
        root = board.report("loc", LEVEL_MINOR, "first").alarm
        board.acknowledge(root.alarm_id)
        followup = board.report("loc", LEVEL_MAJOR, "second").alarm
        cleared = board.resolve(root.alarm_id)
        self.assertEqual({alarm.alarm_id for alarm in cleared}, {root.alarm_id, followup.alarm_id})

    def test_resolve_clears_multi_generation_followup_chain(self) -> None:
        board = AlarmBoard()
        root = board.report("loc", LEVEL_MINOR, "first").alarm
        board.acknowledge(root.alarm_id)
        first_followup = board.report("loc", LEVEL_MAJOR, "second").alarm
        board.acknowledge(first_followup.alarm_id)
        second_followup = board.report("loc", LEVEL_CRITICAL, "third").alarm
        cleared = board.resolve(second_followup.alarm_id)
        self.assertEqual(
            {alarm.alarm_id for alarm in cleared},
            {root.alarm_id, first_followup.alarm_id, second_followup.alarm_id},
        )
        self.assertEqual(board.list_open(), [])

    def test_resolve_location_and_resolve_all(self) -> None:
        board = AlarmBoard()
        board.report("loc/a", LEVEL_MINOR, "a")
        board.report("loc/b", LEVEL_MINOR, "b")
        cleared = board.resolve_location("loc/a")
        self.assertEqual(len(cleared), 1)
        self.assertEqual([alarm.location for alarm in board.list_open()], ["loc/b"])
        cleared = board.resolve_all(operator="dispatcher")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(board.counts()["open"], 0)
        self.assertEqual(board.list_resolved()[0].resolved_by, "dispatcher")

    def test_resolve_already_resolved_is_rejected(self) -> None:
        board = AlarmBoard()
        alarm = board.report("loc", LEVEL_MINOR, "x").alarm
        board.resolve(alarm.alarm_id)
        with self.assertRaises(alerts_module.AlertClosed):
            board.resolve(alarm.alarm_id)
        with self.assertRaises(alerts_module.AlertClosed):
            board.acknowledge(alarm.alarm_id)

    def test_new_report_after_resolve_opens_fresh_alarm(self) -> None:
        board = AlarmBoard()
        first = board.report("loc", LEVEL_MINOR, "x").alarm
        board.resolve(first.alarm_id)
        second = board.report("loc", LEVEL_MINOR, "again")
        self.assertTrue(second.created)
        self.assertNotEqual(second.alarm.alarm_id, first.alarm_id)


class QueryAndAuditTests(unittest.TestCase):
    def test_open_alarms_sorted_by_severity_then_recency(self) -> None:
        board = AlarmBoard()
        board.report("loc/minor", LEVEL_MINOR, "m")
        board.report("loc/crit", LEVEL_CRITICAL, "c")
        board.report("loc/major", LEVEL_MAJOR, "j")
        ordering = [alarm.severity for alarm in board.list_open()]
        self.assertEqual(ordering, [LEVEL_CRITICAL, LEVEL_MAJOR, LEVEL_MINOR])

    def test_lifecycle_is_audited(self) -> None:
        audit = Audit(200)
        board = AlarmBoard()
        alarm = board.report("loc", LEVEL_MINOR, "x", recorder=audit).alarm
        board.escalate_due()
        board.acknowledge(alarm.alarm_id, recorder=audit)
        board.resolve(alarm.alarm_id, recorder=audit)
        actions = [entry.action for entry in audit.query(limit=10)]
        self.assertIn("alarm-raise", actions)
        self.assertIn("alarm-ack", actions)
        self.assertIn("alarm-resolve", actions)

    def test_history_is_bounded(self) -> None:
        board, clock = board_with_timeout()
        board.history_limit = 2
        ids = []
        for index in range(3):
            alarm = board.report(f"loc/{index}", LEVEL_MINOR, str(index)).alarm
            ids.append(alarm.alarm_id)
            board.resolve(alarm.alarm_id)
        self.assertEqual(len(board.list_resolved(limit=10)), 2)
        with self.assertRaises(alerts_module.AlertNotFound):
            board.get(ids[0])


class PersistenceTests(unittest.TestCase):
    def test_round_trip_keeps_open_alarm_state(self) -> None:
        store = MemoryStore()
        board = AlarmBoard()
        alarm = board.report("crane-1/slew-drive", LEVEL_MAJOR, "故障").alarm
        board.report("crane-1/slew-drive", LEVEL_MAJOR, "连锁")
        board.acknowledge(alarm.alarm_id, operator="lao-wang")
        alerts_module.persist(board, store)

        restored = AlarmBoard()
        alerts_module.restore(restored, store)
        reopened = restored.open_for("crane-1/slew-drive")
        self.assertIsNotNone(reopened)
        assert reopened is not None
        self.assertEqual(reopened.status, STATUS_ACKNOWLEDGED)
        self.assertEqual(reopened.repeat_count(), 2)
        self.assertEqual(reopened.acknowledged_by, "lao-wang")

    def test_restore_missing_key_is_fresh_board(self) -> None:
        restored = AlarmBoard()
        alerts_module.restore(restored, MemoryStore())
        self.assertEqual(restored.list_open(), [])

    def test_open_status_after_restart_keeps_escalation_deadline(self) -> None:
        store = MemoryStore()
        board, clock = board_with_timeout({LEVEL_MINOR: 60.0, LEVEL_MAJOR: 60.0})
        alarm = board.report("loc", LEVEL_MINOR, "x").alarm
        clock.advance(30)
        alerts_module.persist(board, store)

        restored = AlarmBoard(clock=clock)
        alerts_module.restore(restored, store)
        clock.advance(31)
        restored.escalate_due()
        self.assertEqual(restored.get(alarm.alarm_id).severity, LEVEL_MAJOR)

    def test_resolved_records_survive_restart(self) -> None:
        store = MemoryStore()
        board = AlarmBoard()
        alarm = board.report("loc", LEVEL_MINOR, "x").alarm
        board.resolve(alarm.alarm_id, operator="lao-wang")
        alerts_module.persist(board, store)

        restored = AlarmBoard()
        alerts_module.restore(restored, store)
        self.assertEqual(len(restored.list_resolved()), 1)
        self.assertIsNone(restored.open_for("loc"))


if __name__ == "__main__":
    unittest.main()
