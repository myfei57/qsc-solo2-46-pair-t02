"""Site alarm board: merged incidents with escalation and acknowledgement.

Raw alarms used to land in the audit log as unrelated lines, so the same part
faulting repeatedly produced a handful of entries nobody could track. The board
gives every alarm an incident lifecycle instead:

* alarms from the same :attr:`Incident.location` merge into one open incident;
* an incident that is not acknowledged in time escalates one severity level;
* an acknowledged incident is never mutated by later reports — a new incident
  is opened and linked back to the acknowledged one;
* resolved incidents stay on the board as history and are never reopened.

Escalation is evaluated lazily (on report/list) so the board needs no timer
thread; the clock is injectable for tests.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

from .errors import AlarmNotFound, AlarmStateError
from .store import Store, StoreNotFound

ALARM_KEY = "alarms/board"

# --------------------------------------------------------------------- levels

SEVERITY_MINOR = "minor"
SEVERITY_MAJOR = "major"
SEVERITY_CRITICAL = "critical"

SEVERITY_ORDER = (SEVERITY_MINOR, SEVERITY_MAJOR, SEVERITY_CRITICAL)
SEVERITY_RANK = {level: index for index, level in enumerate(SEVERITY_ORDER)}

STATUS_ACTIVE = "active"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_RESOLVED = "resolved"
OPEN_STATUSES = (STATUS_ACTIVE, STATUS_ACKNOWLEDGED)

# Default seconds an incident may sit at one severity before it is bumped.
DEFAULT_ESCALATION_SECONDS: dict[str, float] = {
    SEVERITY_MINOR: 300.0,
    SEVERITY_MAJOR: 900.0,
    SEVERITY_CRITICAL: float("inf"),
}

DEFAULT_CAPACITY = 300


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def max_severity(left: str, right: str) -> str:
    """Return the more severe of two severity levels."""

    return left if SEVERITY_RANK[left] >= SEVERITY_RANK[right] else right


@dataclass
class Incident:
    """A merged alarm tied to one physical location."""

    incident_id: str
    location: str
    kind: str
    severity: str
    title: str
    first_seen: str
    last_seen: str
    occurrences: int = 1
    status: str = STATUS_ACTIVE
    escalated_at: str | None = None
    acknowledged_by: str = ""
    acknowledged_at: str | None = None
    resolved_by: str = ""
    resolved_at: str | None = None
    follows: str = ""
    kinds: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.incident_id,
            "location": self.location,
            "kind": self.kind,
            "kinds": list(self.kinds),
            "severity": self.severity,
            "title": self.title,
            "status": self.status,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "occurrences": self.occurrences,
            "escalated_at": self.escalated_at,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
            "follows": self.follows,
            "messages": list(self.messages),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Incident":
        return cls(
            incident_id=str(payload["id"]),
            location=str(payload["location"]),
            kind=str(payload["kind"]),
            severity=str(payload["severity"]),
            title=str(payload["title"]),
            first_seen=str(payload["first_seen"]),
            last_seen=str(payload["last_seen"]),
            occurrences=int(payload.get("occurrences", 1)),  # type: ignore[arg-type]
            status=str(payload.get("status", STATUS_ACTIVE)),
            escalated_at=_optional_str(payload.get("escalated_at")),
            acknowledged_by=str(payload.get("acknowledged_by", "")),
            acknowledged_at=_optional_str(payload.get("acknowledged_at")),
            resolved_by=str(payload.get("resolved_by", "")),
            resolved_at=_optional_str(payload.get("resolved_at")),
            follows=str(payload.get("follows", "")),
            kinds=[str(item) for item in payload.get("kinds", [])],  # type: ignore[arg-type]
            messages=[str(item) for item in payload.get("messages", [])],  # type: ignore[arg-type]
        )


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


Clock = Callable[[], datetime]


class AlarmBoard:
    """Thread-safe collection of merged, escalating incidents."""

    def __init__(
        self,
        escalation_seconds: dict[str, float] | None = None,
        capacity: int = DEFAULT_CAPACITY,
        clock: Clock = _utc_now,
    ) -> None:
        self._deadlines = dict(DEFAULT_ESCALATION_SECONDS)
        if escalation_seconds:
            for level, seconds in escalation_seconds.items():
                if level in SEVERITY_RANK:
                    self._deadlines[level] = float(seconds)
        self._capacity = capacity if capacity > 0 else DEFAULT_CAPACITY
        self._clock = clock
        self._incidents: list[Incident] = []
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def escalation_seconds(self) -> dict[str, float]:
        return dict(self._deadlines)

    def report(
        self,
        location: str,
        kind: str,
        severity: str = SEVERITY_MAJOR,
        title: str = "",
        message: str = "",
    ) -> Incident:
        """Merge a fresh alarm into the board and return the incident.

        Repeated alarms at one location extend the single open *active*
        incident. An already acknowledged incident is left untouched and a new
        incident linked via ``follows`` is opened instead.
        """

        location = location.strip()
        if not location:
            raise ValueError("location required")
        if severity not in SEVERITY_RANK:
            severity = SEVERITY_MAJOR
        now = self._clock()
        stamp = now.isoformat()
        with self._lock:
            self._escalate_locked(now)
            open_incident = self._find_open_locked(location)
            if open_incident is None or open_incident.status == STATUS_ACKNOWLEDGED:
                follows = open_incident.incident_id if open_incident is not None else ""
                incident = Incident(
                    incident_id=uuid.uuid4().hex,
                    location=location,
                    kind=kind,
                    severity=severity,
                    title=title or kind,
                    first_seen=stamp,
                    last_seen=stamp,
                    follows=follows,
                    kinds=[kind],
                    messages=[message] if message else [],
                )
                self._incidents.append(incident)
            else:
                incident = open_incident
                incident.last_seen = stamp
                incident.occurrences += 1
                incident.severity = max_severity(incident.severity, severity)
                incident.title = title or incident.title
                if kind not in incident.kinds:
                    incident.kinds.append(kind)
                if message:
                    incident.messages.append(message)
            self._prune_locked()
            return self._snapshot_incident(incident)

    def acknowledge(self, incident_id: str, operator: str) -> Incident:
        """Mark one open incident acknowledged, freezing it against new reports."""

        operator = operator.strip() or "operator"
        with self._lock:
            incident = self._require_locked(incident_id)
            if incident.status == STATUS_RESOLVED:
                raise AlarmStateError("incident already resolved")
            if incident.status == STATUS_ACKNOWLEDGED:
                raise AlarmStateError("incident already acknowledged")
            incident.status = STATUS_ACKNOWLEDGED
            incident.acknowledged_by = operator
            incident.acknowledged_at = self._clock().isoformat()
            return self._snapshot_incident(incident)

    def resolve(self, incident_id: str, operator: str) -> Incident:
        """Close one incident."""

        operator = operator.strip() or "operator"
        with self._lock:
            incident = self._require_locked(incident_id)
            if incident.status == STATUS_RESOLVED:
                raise AlarmStateError("incident already resolved")
            incident.status = STATUS_RESOLVED
            incident.resolved_by = operator
            incident.resolved_at = self._clock().isoformat()
            return self._snapshot_incident(incident)

    def resolve_batch(
        self,
        operator: str,
        location: str = "",
        kind: str = "",
        include_acknowledged: bool = True,
    ) -> list[Incident]:
        """Resolve every matching open incident in one sweep.

        ``location`` matches the exact part or a parent of it (``crane-1``
        clears ``crane-1/slew`` and ``crane-1/limit/slew``). By default both
        active and acknowledged incidents at the location are closed so a
        handled part can be cleared as a batch.
        """

        operator = operator.strip() or "operator"
        stamp = self._clock().isoformat()
        closed: list[Incident] = []
        with self._lock:
            for incident in self._incidents:
                if not incident.is_open():
                    continue
                if not include_acknowledged and incident.status == STATUS_ACKNOWLEDGED:
                    continue
                if location and not _location_matches(incident.location, location):
                    continue
                if kind and not _matches_kind(incident, kind):
                    continue
                incident.status = STATUS_RESOLVED
                incident.resolved_by = operator
                incident.resolved_at = stamp
                closed.append(incident)
            return [self._snapshot_incident(incident) for incident in closed]

    def auto_resolve(self, location: str, kind: str, operator: str = "system") -> list[Incident]:
        """Close open incidents of one kind at one location once the cause clears.

        Only incidents carrying exactly that alarm kind are touched, so a wind
        clear never silently closes an unrelated limit incident at the site.
        """

        stamp = self._clock().isoformat()
        closed: list[Incident] = []
        with self._lock:
            for incident in self._incidents:
                if not incident.is_open() or incident.kind != kind:
                    continue
                if not _location_matches(incident.location, location):
                    continue
                incident.status = STATUS_RESOLVED
                incident.resolved_by = operator
                incident.resolved_at = stamp
                closed.append(incident)
            return [self._snapshot_incident(incident) for incident in closed]

    def escalate_due(self) -> list[Incident]:
        """Force the timeout check and return every incident that moved up."""

        now = self._clock()
        with self._lock:
            before = {incident.incident_id: incident.severity for incident in self._incidents}
            self._escalate_locked(now)
            moved = [
                self._snapshot_incident(incident)
                for incident in self._incidents
                if before.get(incident.incident_id) != incident.severity
            ]
            return moved

    # ------------------------------------------------------------- queries

    def list(
        self,
        status: str = "",
        location: str = "",
        kind: str = "",
        limit: int = 100,
    ) -> list[Incident]:
        """Return incidents newest-first, applying lazy escalation first."""

        with self._lock:
            self._escalate_locked(self._clock())
            incidents = list(self._incidents)
        collected: list[Incident] = []
        for incident in reversed(incidents):
            if status and incident.status != status:
                continue
            if location and not _location_matches(incident.location, location):
                continue
            if kind and not _matches_kind(incident, kind):
                continue
            collected.append(self._snapshot_incident(incident))
            if limit > 0 and len(collected) >= limit:
                break
        return collected

    def get(self, incident_id: str) -> Incident:
        with self._lock:
            return self._snapshot_incident(self._require_locked(incident_id))

    def open_count(self) -> int:
        with self._lock:
            self._escalate_locked(self._clock())
            return sum(1 for incident in self._incidents if incident.is_open())

    def counts_by_severity(self) -> dict[str, int]:
        with self._lock:
            self._escalate_locked(self._clock())
            counts = {level: 0 for level in SEVERITY_ORDER}
            for incident in self._incidents:
                if incident.is_open():
                    counts[incident.severity] += 1
            return counts

    def snapshot(self) -> list[Incident]:
        with self._lock:
            return [self._snapshot_incident(incident) for incident in self._incidents]

    def restore(self, incidents: Sequence[Incident]) -> None:
        with self._lock:
            self._incidents = list(incidents)
            self._prune_locked()

    # ------------------------------------------------------------- internals

    def _escalate_locked(self, now: datetime) -> None:
        for incident in self._incidents:
            if incident.status != STATUS_ACTIVE:
                continue
            deadline = self._deadlines.get(incident.severity, float("inf"))
            if deadline == float("inf"):
                continue
            reference = _parse(incident.escalated_at or incident.first_seen)
            if reference is None:
                continue
            if (now - reference).total_seconds() < deadline:
                continue
            rank = SEVERITY_RANK[incident.severity]
            if rank + 1 >= len(SEVERITY_ORDER):
                continue
            incident.severity = SEVERITY_ORDER[rank + 1]
            incident.escalated_at = now.isoformat()

    def _find_open_locked(self, location: str) -> Incident | None:
        for incident in reversed(self._incidents):
            if incident.location == location and incident.is_open():
                return incident
        return None

    def _require_locked(self, incident_id: str) -> Incident:
        for incident in self._incidents:
            if incident.incident_id == incident_id:
                return incident
        raise AlarmNotFound(incident_id)

    def _prune_locked(self) -> None:
        if len(self._incidents) <= self._capacity:
            return
        # Keep all open incidents; trim the oldest resolved history only.
        overflow = len(self._incidents) - self._capacity
        kept: list[Incident] = []
        for incident in self._incidents:
            if overflow > 0 and incident.status == STATUS_RESOLVED:
                overflow -= 1
                continue
            kept.append(incident)
        self._incidents = kept
        if len(self._incidents) > self._capacity:
            del self._incidents[: len(self._incidents) - self._capacity]

    @staticmethod
    def _snapshot_incident(incident: Incident) -> Incident:
        return replace(
            incident, kinds=list(incident.kinds), messages=list(incident.messages)
        )


def _matches_kind(incident: Incident, kind: str) -> bool:
    return incident.kind == kind or kind in incident.kinds


def _location_matches(actual: str, wanted: str) -> bool:
    """Match the exact part or any child of a part path (``crane-1/...``)."""

    return actual == wanted or actual.startswith(wanted.rstrip("/") + "/")


def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def incidents_to_json(incidents: Iterable[Incident]) -> list[dict[str, object]]:
    return [incident.to_dict() for incident in incidents]


def save_board(store: Store, board: AlarmBoard) -> None:
    """Persist every incident as one JSON document."""

    payload = json.dumps(
        [incident.to_dict() for incident in board.snapshot()], ensure_ascii=False
    ).encode("utf-8")
    store.save(ALARM_KEY, payload)


def restore_board(store: Store, board: AlarmBoard) -> None:
    """Load a persisted board; a missing key leaves the board empty."""

    try:
        payload = store.load(ALARM_KEY)
    except StoreNotFound:
        return
    raw = json.loads(payload.decode("utf-8"))
    board.restore([Incident.from_dict(item) for item in raw])
