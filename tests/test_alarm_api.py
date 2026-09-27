"""Integration tests for the alarm board exposed through the console API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cranefleet.alarms import (
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    STATUS_ACKNOWLEDGED,
    STATUS_ACTIVE,
    STATUS_RESOLVED,
)
from cranefleet.console import Console, Request
from cranefleet.wiring import build_app

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def dispatch(console: Console, method: str, path: str, body: dict | None = None):  # noqa: ANN201
    request = Request(method=method, path=path, query={}, body=body or {}, body_valid=True)
    return console.dispatch(request)


def open_incidents(console: Console) -> list[dict]:
    status, payload = dispatch(console, "GET", "/api/alarms")
    assert status == 200
    return [item for item in payload["data"]["incidents"] if item["status"] != STATUS_RESOLVED]


class AlarmApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_app("memory", web_root=str(WEB_ROOT))
        self.console = self.app.console

    def tearDown(self) -> None:
        self.app.shutdown()

    def test_wind_alarms_merge_into_one_incident(self) -> None:
        dispatch(self.console, "POST", "/api/slew/start")
        for level in (14, 15, 16):
            status, _ = dispatch(self.console, "POST", "/api/wind/alarm", {"level": level})
            self.assertEqual(status, 200)
        incidents = open_incidents(self.console)
        wind = [item for item in incidents if item["kind"] == "wind"]
        self.assertEqual(len(wind), 1)
        self.assertEqual(wind[0]["occurrences"], 3)
        self.assertEqual(wind[0]["location"], "site/wind")
        self.assertEqual(wind[0]["severity"], SEVERITY_MAJOR)
        status, payload = dispatch(self.console, "GET", "/api/state")
        self.assertEqual(payload["data"]["alarms"], 1)

    def test_acknowledged_incident_is_not_overwritten(self) -> None:
        dispatch(self.console, "POST", "/api/slew/start")
        dispatch(self.console, "POST", "/api/wind/alarm", {"level": 14})
        first = open_incidents(self.console)[0]
        status, payload = dispatch(
            self.console, "POST", "/api/alarms/acknowledge", {"id": first["id"], "operator": "zhang"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["status"], STATUS_ACKNOWLEDGED)
        dispatch(self.console, "POST", "/api/wind/alarm", {"level": 16})
        incidents = self.console.alarms.list(location="site/wind")
        self.assertEqual(len(incidents), 2)
        original = next(item for item in incidents if item.incident_id == first["id"])
        follow_up = next(item for item in incidents if item.incident_id != first["id"])
        self.assertEqual(original.status, STATUS_ACKNOWLEDGED)
        self.assertEqual(original.occurrences, 1)
        self.assertEqual(follow_up.follows, first["id"])
        self.assertEqual(follow_up.status, STATUS_ACTIVE)

    def test_batch_clear_resolves_whole_location(self) -> None:
        dispatch(self.console, "POST", "/api/slew/start")
        dispatch(self.console, "POST", "/api/wind/alarm", {"level": 14})
        dispatch(self.console, "POST", "/api/slew/fault", {"faulted": True})
        status, payload = dispatch(
            self.console, "POST", "/api/alarms/clear", {"location": "site", "operator": "li"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["resolved"], 1)
        self.assertEqual(self.console.alarms.open_count(), 1)
        status, payload = dispatch(self.console, "POST", "/api/alarms/clear", {"kind": "slew-fault"})
        self.assertEqual(payload["data"]["resolved"], 1)
        self.assertEqual(self.console.alarms.open_count(), 0)

    def test_clear_requires_scope(self) -> None:
        status, _ = dispatch(self.console, "POST", "/api/alarms/clear", {})
        self.assertEqual(status, 400)

    def test_acknowledge_unknown_id_returns_404(self) -> None:
        status, payload = dispatch(
            self.console, "POST", "/api/alarms/acknowledge", {"id": "nope"}
        )
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_double_acknowledge_returns_409(self) -> None:
        dispatch(self.console, "POST", "/api/slew/start")
        dispatch(self.console, "POST", "/api/wind/alarm", {"level": 14})
        incident_id = open_incidents(self.console)[0]["id"]
        dispatch(self.console, "POST", "/api/alarms/acknowledge", {"id": incident_id})
        status, payload = dispatch(
            self.console, "POST", "/api/alarms/acknowledge", {"id": incident_id}
        )
        self.assertEqual(status, 409)

    def test_zone_conflict_raises_critical_and_clears_with_zone(self) -> None:
        dispatch(self.console, "POST", "/api/cluster/survey", {"cranes": {"crane-2": {"x": 5, "y": 0}}})
        dispatch(self.console, "POST", "/api/zone/occupy", {"crane_id": "crane-1"})
        dispatch(self.console, "POST", "/api/zone/occupy", {"crane_id": "crane-2"})
        conflicts = [
            item
            for item in open_incidents(self.console)
            if item["kind"] == "zone-conflict"
        ]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["severity"], SEVERITY_CRITICAL)
        dispatch(self.console, "POST", "/api/zone/clear")
        statuses = [
            item.status for item in self.console.alarms.list(kind="zone-conflict")
        ]
        self.assertEqual(statuses, [STATUS_RESOLVED])

    def test_limit_violations_are_minor_per_axis(self) -> None:
        dispatch(self.console, "POST", "/api/cluster/survey", {"cranes": {"crane-2": {"x": 0, "y": 150}}})
        dispatch(self.console, "POST", "/api/limit/check")
        limit_alarms = [item for item in open_incidents(self.console) if item["kind"] == "limit-trolley"]
        self.assertEqual(len(limit_alarms), 1)
        self.assertEqual(limit_alarms[0]["severity"], SEVERITY_MINOR)

    def test_slew_recovery_auto_resolves_fault_incident(self) -> None:
        dispatch(self.console, "POST", "/api/slew/fault", {"faulted": True})
        self.assertEqual(self.console.alarms.open_count(), 1)
        dispatch(self.console, "POST", "/api/slew/fault", {"faulted": False})
        self.assertEqual(self.console.alarms.open_count(), 0)
        incidents = self.console.alarms.list(kind="slew-fault")
        self.assertEqual(incidents[0].status, STATUS_RESOLVED)


class AlarmPersistenceTests(unittest.TestCase):
    def test_board_survives_restart_with_ack_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = build_app(directory, web_root=str(WEB_ROOT))
            dispatch(first.console, "POST", "/api/slew/start")
            dispatch(first.console, "POST", "/api/wind/alarm", {"level": 14})
            dispatch(first.console, "POST", "/api/wind/alarm", {"level": 15})
            incident_id = first.console.alarms.list(kind="wind")[0].incident_id
            first.console.alarms.acknowledge(incident_id, "zhang")
            first.shutdown()

            second = build_app(directory, web_root=str(WEB_ROOT))
            incidents = second.console.alarms.list(kind="wind")
            self.assertEqual(len(incidents), 1)
            self.assertEqual(incidents[0].occurrences, 2)
            self.assertEqual(incidents[0].status, STATUS_ACKNOWLEDGED)
            self.assertEqual(incidents[0].acknowledged_by, "zhang")
            # A new post-restart report must not overwrite the acknowledged record.
            dispatch(second.console, "POST", "/api/wind/alarm", {"level": 16})
            self.assertEqual(len(second.console.alarms.list(kind="wind")), 2)
            second.shutdown()


if __name__ == "__main__":
    unittest.main()
