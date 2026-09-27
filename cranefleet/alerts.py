"""Site alarm board: merging, acknowledgement and automatic escalation.

Alarms are keyed by the physical *location* they come from (a crane, a zone,
an axis). Every alarm reported for the same location while one is still open is
merged into that single record instead of flooding the list: the repeat count
grows, the message/source of the newest occurrence is kept and a more severe
report raises the level. Once an alarm has been acknowledged it is "owned" by
that operator and a later report for the same location does not overwrite it;
the later report opens a follow-up alarm linked back to the original.

An unacknowledged alarm is escalated one severity level when it has stayed open
longer than the timeout for its current level. Escalation is evaluated lazily
whenever the board is touched, so no background timer is needed. Resolving an
alarm also resolves every alarm merged into its chain, which lets an operator
batch-clear a whole fault in one call.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .audit import Audit
from .errors import AlertClosed, AlertNotFound, StoreNotFound
from .store import Store

LEVEL_MINOR = "minor"
LEVEL_MAJOR = "major"
LEVEL_CRITICAL = "critical"
SEVERITY_LEVELS = (LEVEL_MINOR, LEVEL_MAJOR, LEVEL_CRITICAL)
SEVERITY_RANK = {level: index for index, level in enumerate(SEVERITY_LEVELS)}

STATUS_OPEN = "open"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_RESOLVED = "resolved"

DEFAULT_ESCALATION_TIMEOUTS = {
    LEVEL_MINOR: 300.0,
    LEVEL_MAJOR: 600.0,
    LEVEL_CRITICAL: 0.0,
}
DEFAULT_HISTORY_LIMIT = 200
OCCURRENCE_LIMIT = 20


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_severity(level: str) -> str:
    if level not in SEVERITY_RANK:
        raise ValueError(f"unknown severity: {level}")
    return level


@dataclass(frozen=True)
class Occurrence:
    """One raw report folded into an alarm."""

    at: str
    source: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"at": self.at, "source": self.source, "message": self.message}

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> "Occurrence":
        return cls(at=payload["at"], source=payload.get("source", ""), message=payload.get("message", ""))


@dataclass
class Alarm:
    """A single open/resolved alarm for one physical location."""

    alarm_id: str
    location: str
    severity: str
    source: str
    message: str
    occurrences: list[Occurrence] = field(default_factory=list)
    status: str = STATUS_OPEN
    first_seen: str = ""
    last_seen: str = ""
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None
    escalations: int = 0
    escalate_after: str | None = None
    root_id: str | None = None
    followup_id: str | None = None

    def repeat_count(self) -> int:
        return len(self.occurrences)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.alarm_id,
            "location": self.location,
            "severity": self.severity,
            "source": self.source,
            "message": self.message,
            "status": self.status,
            "repeats": self.repeat_count(),
            "occurrences": [item.to_dict() for item in self.occurrences],
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_by": self.acknowledged_by,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "escalations": self.escalations,
            "escalate_after": self.escalate_after,
            "root_id": self.root_id,
            "followup_id": self.followup_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Alarm":
        return cls(
            alarm_id=str(payload["id"]),
            location=str(payload["location"]),
            severity=normalize_severity(str(payload["severity"])),
            source=str(payload.get("source", "")),
            message=str(payload.get("message", "")),
            occurrences=[
                Occurrence.from_dict(item)
                for item in payload.get("occurrences", [])  # type: ignore[arg-type]
                if isinstance(item, dict)
            ],
            status=str(payload.get("status", STATUS_OPEN)),
            first_seen=str(payload.get("first_seen", "")),
            last_seen=str(payload.get("last_seen", "")),
            acknowledged_at=payload.get("acknowledged_at") or None,
            acknowledged_by=payload.get("acknowledged_by") or None,
            resolved_at=payload.get("resolved_at") or None,
            resolved_by=payload.get("resolved_by") or None,
            escalations=int(payload.get("escalations", 0)),
            escalate_after=payload.get("escalate_after") or None,
            root_id=payload.get("root_id") or None,
            followup_id=payload.get("followup_id") or None,
        )


@dataclass(frozen=True)
class ReportOutcome:
    """What happened when one raw alarm report hit the board."""

    alarm: Alarm
    created: bool
    merged: bool
    raised: bool
    followup: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "alarm": self.alarm.to_dict(),
            "created": self.created,
            "merged": self.merged,
            "raised": self.raised,
            "followup": self.followup,
        }


Clock = Callable[[], datetime]


class AlarmBoard:
    """Thread-safe collection of site alarms keyed by location."""

    def __init__(
        self,
        timeouts: dict[str, float] | None = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        clock: Clock = utc_now,
    ) -> None:
        configured = dict(DEFAULT_ESCALATION_TIMEOUTS)
        if timeouts:
            for level, seconds in timeouts.items():
                configured[normalize_severity(level)] = float(seconds)
        self.timeouts = configured
        self.history_limit = history_limit if history_limit > 0 else DEFAULT_HISTORY_LIMIT
        self._clock = clock
        self._lock = threading.Lock()
        self._alarms: dict[str, Alarm] = {}
        self._open_by_location: dict[str, str] = {}

    # ------------------------------------------------------------- reporting

    def report(
        self,
        location: str,
        severity: str,
        message: str,
        source: str = "system",
        recorder: Audit | None = None,
    ) -> ReportOutcome:
        """Merge a new report into the location's open alarm or open one."""

        location = str(location or "").strip()
        if not location:
            raise ValueError("location required")
        severity = normalize_severity(severity)
        with self._lock:
            self._escalate_due_locked(recorder)
            now = self._clock()
            existing_id = self._open_by_location.get(location)
            existing = self._alarms.get(existing_id) if existing_id is not None else None
            if existing is not None and existing.status == STATUS_ACKNOWLEDGED:
                # An owned alarm is never overwritten; the new report becomes a
                # linked follow-up that shows up as its own row.
                alarm = self._create_locked(location, severity, message, source, now, root=existing)
                existing.followup_id = alarm.alarm_id
                if recorder is not None:
                    recorder.record(
                        location, "alarm-followup", f"{alarm.alarm_id} after {existing.alarm_id}"
                    )
                return ReportOutcome(alarm=alarm, created=True, merged=False, raised=False, followup=True)
            if existing is not None:
                return self._merge_locked(existing, severity, message, source, now, recorder)
            alarm = self._create_locked(location, severity, message, source, now)
            if recorder is not None:
                recorder.record(location, "alarm-raise", f"{severity} {alarm.alarm_id}")
            return ReportOutcome(alarm=alarm, created=True, merged=False, raised=False, followup=False)

    def _create_locked(
        self,
        location: str,
        severity: str,
        message: str,
        source: str,
        now: datetime,
        root: Alarm | None = None,
    ) -> Alarm:
        timestamp = now.isoformat()
        alarm = Alarm(
            alarm_id=uuid.uuid4().hex,
            location=location,
            severity=severity,
            source=source,
            message=message,
            status=STATUS_OPEN,
            first_seen=timestamp,
            last_seen=timestamp,
            occurrences=[Occurrence(at=timestamp, source=source, message=message)],
            escalate_after=self._deadline(severity, now),
            root_id=root.alarm_id if root is not None else None,
        )
        self._alarms[alarm.alarm_id] = alarm
        self._open_by_location[location] = alarm.alarm_id
        return alarm

    def _merge_locked(
        self,
        alarm: Alarm,
        severity: str,
        message: str,
        source: str,
        now: datetime,
        recorder: Audit | None,
    ) -> ReportOutcome:
        timestamp = now.isoformat()
        alarm.occurrences.append(Occurrence(at=timestamp, source=source, message=message))
        if len(alarm.occurrences) > OCCURRENCE_LIMIT:
            del alarm.occurrences[:-OCCURRENCE_LIMIT]
        alarm.last_seen = timestamp
        alarm.source = source
        alarm.message = message
        raised = SEVERITY_RANK[severity] > SEVERITY_RANK[alarm.severity]
        if raised:
            alarm.severity = severity
            alarm.escalate_after = self._deadline(severity, now)
            if recorder is not None:
                recorder.record(alarm.location, "alarm-raise", f"raised to {severity}")
        return ReportOutcome(alarm=alarm, created=False, merged=True, raised=raised, followup=False)

    # -------------------------------------------------------- acknowledgement

    def acknowledge(self, alarm_id: str, operator: str = "operator", recorder: Audit | None = None) -> Alarm:
        """Claim an open alarm; acknowledged alarms stop escalating."""

        with self._lock:
            self._escalate_due_locked(recorder)
            alarm = self._require_open_locked(alarm_id)
            if alarm.status == STATUS_OPEN:
                alarm.status = STATUS_ACKNOWLEDGED
                alarm.acknowledged_at = self._clock().isoformat()
                alarm.acknowledged_by = operator
                alarm.escalate_after = None
                if recorder is not None:
                    recorder.record(alarm.location, "alarm-ack", f"{alarm_id} by {operator}")
            return alarm

    def resolve(
        self,
        alarm_id: str,
        operator: str = "operator",
        recorder: Audit | None = None,
    ) -> list[Alarm]:
        """Resolve an alarm and every alarm chained to it.

        The chain covers its follow-ups (reports that arrived after it was
        acknowledged) and, when resolving a follow-up, its acknowledged root,
        so clearing the current fault clears the whole incident in one call.
        """

        with self._lock:
            self._escalate_due_locked(recorder)
            alarm = self._require_open_locked(alarm_id)
            chain = self._chain_locked(alarm)
            timestamp = self._clock().isoformat()
            for member in chain:
                if member.status == STATUS_RESOLVED:
                    continue
                member.status = STATUS_RESOLVED
                member.resolved_at = timestamp
                member.resolved_by = operator
                member.escalate_after = None
                if self._open_by_location.get(member.location) == member.alarm_id:
                    del self._open_by_location[member.location]
                if recorder is not None:
                    recorder.record(member.location, "alarm-resolve", f"{member.alarm_id} by {operator}")
            self._trim_history_locked()
            return chain

    def resolve_location(
        self,
        location: str,
        operator: str = "operator",
        recorder: Audit | None = None,
    ) -> list[Alarm]:
        """Resolve the open alarm (and its chain) for one location."""

        with self._lock:
            alarm_id = self._open_by_location.get(location)
            if alarm_id is None:
                return []
        return self.resolve(alarm_id, operator, recorder)

    def resolve_all(self, operator: str = "operator", recorder: Audit | None = None) -> list[Alarm]:
        """Batch-resolve every open alarm."""

        with self._lock:
            open_ids = list(self._open_by_location.values())
        resolved: list[Alarm] = []
        for alarm_id in open_ids:
            try:
                resolved.extend(self.resolve(alarm_id, operator, recorder))
            except AlertClosed:  # pragma: no cover - resolved via another chain
                continue
        # de-duplicate while preserving order
        unique: dict[str, Alarm] = {}
        for alarm in resolved:
            unique.setdefault(alarm.alarm_id, alarm)
        return list(unique.values())

    # ----------------------------------------------------------- escalation

    def escalate_due(self, recorder: Audit | None = None) -> list[Alarm]:
        """Escalate every unacknowledged alarm whose timeout has elapsed."""

        with self._lock:
            return self._escalate_due_locked(recorder)

    def _escalate_due_locked(self, recorder: Audit | None) -> list[Alarm]:
        now = self._clock()
        escalated: list[Alarm] = []
        for alarm_id in list(self._open_by_location.values()):
            alarm = self._alarms.get(alarm_id)
            if alarm is None or alarm.status != STATUS_OPEN or alarm.escalate_after is None:
                continue
            if now < datetime.fromisoformat(alarm.escalate_after):
                continue
            next_level = _next_severity(alarm.severity)
            if next_level is None:
                alarm.escalate_after = None
                continue
            alarm.severity = next_level
            alarm.escalations += 1
            alarm.escalate_after = self._deadline(next_level, now)
            escalated.append(alarm)
            if recorder is not None:
                recorder.record(alarm.location, "alarm-escalate", f"{alarm_id} to {next_level}")
        return escalated

    def _deadline(self, severity: str, now: datetime) -> str | None:
        seconds = self.timeouts.get(severity, 0.0)
        if seconds <= 0:
            return None
        return (now + timedelta(seconds=seconds)).isoformat()

    # ---------------------------------------------------------------- queries

    def get(self, alarm_id: str) -> Alarm:
        with self._lock:
            return self._require_locked(alarm_id)

    def open_for(self, location: str) -> Alarm | None:
        with self._lock:
            alarm_id = self._open_by_location.get(location)
            return self._alarms[alarm_id] if alarm_id is not None else None

    def list_open(self) -> list[Alarm]:
        """Open alarms, most severe first and newest activity first."""

        return self._list(statuses=(STATUS_OPEN, STATUS_ACKNOWLEDGED))

    def list_resolved(self, limit: int | None = None) -> list[Alarm]:
        return self._list(statuses=(STATUS_RESOLVED,), limit=limit)

    def _list(
        self,
        statuses: Iterable[str],
        limit: int | None = None,
        location: str = "",
    ) -> list[Alarm]:
        wanted = set(statuses)
        with self._lock:
            self._escalate_due_locked(None)
            alarms = [
                alarm
                for alarm in self._alarms.values()
                if alarm.status in wanted and (not location or alarm.location == location)
            ]
        alarms.sort(
            key=lambda alarm: (
                SEVERITY_RANK[alarm.severity],
                alarm.last_seen,
            ),
            reverse=True,
        )
        return alarms if limit is None else alarms[:limit]

    def counts(self) -> dict[str, object]:
        with self._lock:
            self._escalate_due_locked(None)
            open_alarms = [
                self._alarms[alarm_id]
                for alarm_id in self._open_by_location.values()
                if alarm_id in self._alarms
            ]
        by_severity = {level: 0 for level in SEVERITY_LEVELS}
        unacknowledged = 0
        for alarm in open_alarms:
            by_severity[alarm.severity] += 1
            if alarm.status == STATUS_OPEN:
                unacknowledged += 1
        return {
            "open": len(open_alarms),
            "unacknowledged": unacknowledged,
            "by_severity": by_severity,
        }

    # ------------------------------------------------------------ internals

    def _require_locked(self, alarm_id: str) -> Alarm:
        alarm = self._alarms.get(alarm_id)
        if alarm is None:
            raise AlertNotFound(alarm_id)
        return alarm

    def _require_open_locked(self, alarm_id: str) -> Alarm:
        alarm = self._require_locked(alarm_id)
        if alarm.status == STATUS_RESOLVED:
            raise AlertClosed(alarm_id)
        return alarm

    def _chain_locked(self, alarm: Alarm) -> list[Alarm]:
        """Collect the alarm, its follow-ups and its acknowledged root."""

        chain: dict[str, Alarm] = {alarm.alarm_id: alarm}
        cursor = alarm
        while cursor.root_id and cursor.root_id not in chain:
            root = self._alarms.get(cursor.root_id)
            if root is None:
                break
            chain[root.alarm_id] = root
            cursor = root
        for member in self._alarms.values():
            if member.root_id in chain and member.alarm_id not in chain:
                chain[member.alarm_id] = member
        return list(chain.values())

    def _trim_history_locked(self) -> None:
        resolved = [alarm for alarm in self._alarms.values() if alarm.status == STATUS_RESOLVED]
        excess = len(resolved) - self.history_limit
        if excess <= 0:
            return
        resolved.sort(key=lambda alarm: alarm.resolved_at or alarm.last_seen)
        for alarm in resolved[:excess]:
            self._alarms.pop(alarm.alarm_id, None)

    # ---------------------------------------------------------- persistence

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [alarm.to_dict() for alarm in self._alarms.values()]

    def restore(self, payload: list[dict[str, object]]) -> None:
        with self._lock:
            self._alarms = {}
            self._open_by_location = {}
            for item in payload:
                try:
                    alarm = Alarm.from_dict(item)
                except (KeyError, TypeError, ValueError):
                    continue
                self._alarms[alarm.alarm_id] = alarm
                if alarm.status != STATUS_RESOLVED:
                    self._open_by_location.setdefault(alarm.location, alarm.alarm_id)
            self._trim_history_locked()


def _next_severity(severity: str) -> str | None:
    rank = SEVERITY_RANK[severity]
    if rank + 1 >= len(SEVERITY_LEVELS):
        return None
    return SEVERITY_LEVELS[rank + 1]


ALERT_KEY = "alerts/board"


def persist(board: AlarmBoard, store: Store) -> None:
    """Durably write the board so a restart keeps every open alarm."""

    store.save(ALERT_KEY, json.dumps(board.snapshot(), ensure_ascii=False).encode("utf-8"))


def restore(board: AlarmBoard, store: Store) -> None:
    """Reload a board written by :func:`persist`; a missing key is a fresh board."""

    try:
        payload = store.load(ALERT_KEY)
    except StoreNotFound:
        return
    board.restore(json.loads(payload.decode("utf-8")))
