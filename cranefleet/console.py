"""The control console: wiring, routing and request handling.

Every HTTP route funnels into one method here so the interlocks stay in a
single place. Handlers return ``(status_code, envelope)`` where the envelope
is the ``{"ok": ...}`` document served to the console frontend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from . import cluster as fleet
from . import config as site_config
from . import crane as crane_module
from . import hoist as hoist_module
from . import limit as limit_module
from . import slew as slew_module
from . import trolley as trolley_module
from . import views
from . import wind as wind_module
from . import zone as zone_module
from . import alarms as alarms_module
from .alarms import AlarmBoard
from .audit import Audit
from .cluster import Cluster
from .config import ConfigError, SiteConfig
from .crane import Crane
from .errors import (
    AlarmNotFound,
    AlarmStateError,
    CraneFleetError,
    FeedbackNotConfirmed,
    HoistBusy,
    HookNotLowered,
    SlewBlocked,
    SlewBusy,
    SlewIdle,
    SlewStopNotAcknowledged,
    StoreError,
    ZoneNotDurable,
)
from .hoist import Drum, Hoist
from .limit import Limit
from .slew import Ramp, Slew, SpeedFeedback
from .store import Store
from .trolley import Speedometer, Trolley
from .wind import Gust, Wind
from .zone import ZoneState

Handler = Callable[["Request"], tuple[int, dict[str, Any]]]

LOGGER = logging.getLogger("cranefleet.console")


@dataclass(frozen=True)
class Request:
    """A parsed HTTP request handed to a console handler."""

    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    body_valid: bool = True

    def query_first(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        if not values:
            return default
        return values[0]

    def query_all(self, name: str) -> list[str]:
        return list(self.query.get(name, []))


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


class Console:
    """Owns every control component and serves the console API."""

    def __init__(
        self,
        store: Store,
        cluster: Cluster,
        zone: ZoneState,
        slew: Slew,
        trolley: Trolley,
        hoist: Hoist,
        wind: Wind,
        limit: Limit,
        audit: Audit,
        alarms: AlarmBoard,
        cranes: dict[str, Crane],
        feedback: SpeedFeedback,
        config: SiteConfig,
        web_root: str = "web",
    ) -> None:
        self.store = store
        self.cluster = cluster
        self.zone = zone
        self.slew = slew
        self.trolley = trolley
        self.hoist = hoist
        self.wind = wind
        self.limit = limit
        self.audit = audit
        self.alarms = alarms
        self.cranes = cranes
        self.feedback = feedback
        self.config = config
        self.web_root = web_root
        self.ramp = Ramp()
        self.drum = Drum(0.8)
        self.speedometer = Speedometer()
        self.gust = Gust()
        self.load = crane_module.Load(2.4)
        self._routes: dict[tuple[str, str], Handler] = {
            ("GET", "/api/state"): self.handle_state,
            ("GET", "/api/zone/state"): self.handle_zone_state,
            ("POST", "/api/zone/occupy"): self.handle_zone_occupy,
            ("POST", "/api/zone/vacate"): self.handle_zone_vacate,
            ("POST", "/api/zone/clear"): self.handle_zone_clear,
            ("GET", "/api/cluster/survey"): self.handle_cluster_survey,
            ("POST", "/api/cluster/survey"): self.handle_cluster_survey_post,
            ("POST", "/api/slew/start"): self.handle_slew_start,
            ("POST", "/api/slew/stop"): self.handle_slew_stop,
            ("POST", "/api/slew/target"): self.handle_slew_target,
            ("POST", "/api/slew/fault"): self.handle_slew_fault,
            ("POST", "/api/wind/alarm"): self.handle_wind_alarm,
            ("POST", "/api/wind/clear"): self.handle_wind_clear,
            ("POST", "/api/crane/command"): self.handle_crane_command,
            ("GET", "/api/crane/state"): self.handle_crane_state,
            ("GET", "/api/crane/load"): self.handle_crane_load,
            ("POST", "/api/crane/reset"): self.handle_crane_reset,
            ("GET", "/api/trolley/state"): self.handle_trolley_state,
            ("POST", "/api/trolley/resume"): self.handle_trolley_resume,
            ("GET", "/api/hoist/state"): self.handle_hoist_state,
            ("POST", "/api/hoist/payout"): self.handle_hoist_payout,
            ("POST", "/api/hoist/feedback"): self.handle_hoist_feedback,
            ("POST", "/api/hoist/release"): self.handle_hoist_release,
            ("POST", "/api/hoist/stop"): self.handle_hoist_stop,
            ("GET", "/api/limit/state"): self.handle_limit_state,
            ("POST", "/api/limit/check"): self.handle_limit_check,
            ("GET", "/api/config"): self.handle_config,
            ("POST", "/api/config"): self.handle_config_save,
            ("GET", "/api/audit"): self.handle_audit,
            ("GET", "/api/alarms"): self.handle_alarms,
            ("POST", "/api/alarms/acknowledge"): self.handle_alarm_acknowledge,
            ("POST", "/api/alarms/resolve"): self.handle_alarm_resolve,
            ("POST", "/api/alarms/clear"): self.handle_alarm_clear,
        }

    @property
    def routes(self) -> dict[tuple[str, str], Handler]:
        return self._routes

    def default_crane(self) -> Crane | None:
        return self.cranes.get("crane-1")

    def crane(self, crane_id: str) -> Crane | None:
        if not crane_id:
            return self.default_crane()
        return self.cranes.get(crane_id)

    def dispatch(self, request: Request) -> tuple[int, dict[str, Any]]:
        handler = self._routes.get((request.method, request.path))
        if handler is None:
            return 404, fail("not found")
        return handler(request)

    # ------------------------------------------------------------------ state

    def handle_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.state_view(self))

    def handle_zone_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.zone_view(self))

    def handle_zone_occupy(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        crane_id = str(request.body.get("crane_id") or "")
        if not crane_id:
            return 400, fail("crane_id required")
        zone_module.occupy(self.zone, crane_id)
        try:
            zone_module.persist(self.zone, self.store)
        except StoreError as exc:
            return 500, fail(str(exc))
        conflicts, mismatches = fleet.check_conflicts(
            self.cluster, self.zone, self.config.safe_separation
        )
        if conflicts:
            zone_module.register_conflict(self.zone, conflicts[0].crane_ids)
            zone_module.lock_zone(self.zone)
            zone_module.persist(self.zone, self.store)
            self.audit.record(crane_id, "zone-conflict", "pair detected")
            pair = ",".join(conflicts[0].crane_ids)
            self._raise_alarm(
                location=self.zone.id,
                kind="zone-conflict",
                severity=alarms_module.SEVERITY_CRITICAL,
                title=f"防碰撞区冲突 {self.zone.name}",
                message=f"同区塔吊: {pair}",
            )
        if mismatches:
            self.audit.record(crane_id, "occupancy-reconcile", "mismatch")
        return 200, ok(
            {
                "occupants": zone_module.occupants_snapshot(self.zone),
                "conflicts": [conflict.to_dict() for conflict in conflicts],
                "mismatches": mismatches,
            }
        )

    def handle_zone_vacate(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        crane_id = str(request.body.get("crane_id") or "")
        zone_module.vacate(self.zone, crane_id)
        try:
            zone_module.persist(self.zone, self.store)
        except StoreError as exc:
            LOGGER.debug("best-effort zone persist skipped after vacate: %s", exc)
        return 200, ok(zone_module.occupants_snapshot(self.zone))

    def handle_zone_clear(self, request: Request) -> tuple[int, dict[str, Any]]:
        generation = zone_module.clear_conflict(self.zone, self.cluster)
        for crane_id in self.cranes:
            if fleet.is_released(self.cluster, crane_id):
                slew_module.release_latch_hold(self.slew)
        try:
            zone_module.persist(self.zone, self.store)
        except StoreError as exc:
            LOGGER.debug("best-effort zone persist skipped after clear: %s", exc)
        self.audit.record("system", "zone-clear", f"{self.zone.id} generation={generation}")
        self._auto_clear_alarms(self.zone.id, "zone-conflict")
        return 200, ok(views.zone_view(self))

    # ---------------------------------------------------------------- cluster

    def handle_cluster_survey(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.cluster_view(self))

    def handle_cluster_survey_post(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        points = _parse_points(request.body.get("cranes"))
        if points is None:
            return 400, fail("bad request")
        fleet.survey(self.cluster, points)
        try:
            fleet.refresh_baseline(self.cluster, self.store)
        except StoreError as exc:
            return 500, fail(str(exc))
        self.audit.record("system", "cluster-survey", "baseline refreshed")
        return 200, ok(views.cluster_view(self))

    # ------------------------------------------------------------------- slew

    def handle_slew_start(self, request: Request) -> tuple[int, dict[str, Any]]:
        if zone_module.is_latched(self.zone):
            return 409, fail("zone latch held")
        try:
            slew_module.start(self.slew, self.zone, self.store, self.audit)
        except ZoneNotDurable:
            return 409, fail("zone state not durable")
        except SlewBlocked:
            return 409, fail("slew blocked by interlock")
        except SlewBusy:
            return 409, fail("slew already moving")
        except CraneFleetError as exc:
            return 409, fail(str(exc))
        return 200, ok(views.slew_view(self))

    def handle_slew_stop(self, request: Request) -> tuple[int, dict[str, Any]]:
        slew_module.stop(self.slew, self.audit)
        return 200, ok(views.slew_view(self))

    def handle_slew_target(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        self.ramp.set_target(_number(request.body.get("angle")))
        self.ramp.step(0.1)
        return 200, ok(views.slew_view(self))

    def handle_slew_fault(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        faulted = bool(request.body.get("faulted", True))
        slew_module.set_fault(self.slew, faulted)
        self.audit.record(self.slew.id, "slew-fault", f"faulted={str(faulted).lower()}")
        if faulted:
            self._raise_alarm(
                location=f"{self.slew.id}/slew",
                kind="slew-fault",
                severity=alarms_module.SEVERITY_MAJOR,
                title="回转驱动故障",
                message="drive fault reported",
            )
        else:
            self._auto_clear_alarms(f"{self.slew.id}/slew", "slew-fault")
        return 200, ok(views.slew_view(self))

    # ------------------------------------------------------------------- wind

    def handle_wind_alarm(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        level = _number(request.body.get("level"))
        wind_module.set_level(self.wind, level)
        self.gust.observe(level)
        wind_module.latch_limits(self.wind, self.limit)
        try:
            sequence = wind_module.alarm(self.wind, self.slew, self.hoist, self.audit)
        except SlewStopNotAcknowledged:
            return 409, fail("slew stop was not acknowledged")
        self._raise_alarm(
            location="site/wind",
            kind="wind",
            severity=alarms_module.SEVERITY_MAJOR,
            title="大风报警",
            message=f"sequence={sequence} level={level:.1f} m/s",
        )
        return 200, ok(views.wind_view(self))

    def handle_wind_clear(self, request: Request) -> tuple[int, dict[str, Any]]:
        wind_module.clear(self.wind, self.limit)
        slew_module.release_wind_hold(self.slew)
        self._auto_clear_alarms("site/wind", "wind")
        return 200, ok(views.wind_view(self))

    # ------------------------------------------------------------------ crane

    def handle_crane_command(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        crane = self.crane(str(request.body.get("crane_id") or ""))
        if crane is None:
            return 404, fail("crane not found")
        command = crane_module.Command.from_dict(request.body)
        if command.source == crane_module.SOURCE_REMOTE:
            crane_module.apply_remote(crane, command)
        else:
            crane_module.apply_cab(crane, command)
        self._book_keeping(command)
        try:
            crane_module.execute(crane, self.slew, self.trolley, self.hoist, self.audit)
        except SlewIdle:
            return 409, fail("slew is idle")
        except HookNotLowered:
            return 409, fail("hook not lowered")
        except HoistBusy:
            return 409, fail("hoist busy")
        if command.action == crane_module.ACTION_LOWER:
            try:
                hoist_module.persist_hoist(self.hoist, self.store)
            except StoreError as exc:
                LOGGER.debug("best-effort hoist persist skipped after lower: %s", exc)
        return 200, ok(crane.last_command().to_dict())

    def _book_keeping(self, command: crane_module.Command) -> None:
        if command.action == crane_module.ACTION_SWING:
            self.ramp.set_target(command.angle)
            self.load.update(0.3, 0.1)
        elif command.action == crane_module.ACTION_LOWER:
            self.drum.pay_out(1.5)
            self.load.set_vertical(8.0)
        elif command.action == crane_module.ACTION_TROLLEY:
            self.speedometer.sample(self.trolley.position_now(), 0.5)
        elif command.action == crane_module.ACTION_STOP:
            self.ramp.set_target(0.0)
            self.load.update(-0.2, 0.1)

    def handle_crane_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.crane_view(self))

    def handle_crane_load(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.load_view(self))

    def handle_crane_reset(self, request: Request) -> tuple[int, dict[str, Any]]:
        crane = self.default_crane()
        if crane is None:
            return 404, fail("crane not found")
        crane.clear_stop()
        limit_module.reset_arbitration(self.limit)
        self.audit.record(crane.id, "command-reset", "stops cleared")
        return 200, ok(crane.last_command().to_dict())

    # -------------------------------------------------------- trolley / hoist

    def handle_trolley_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.trolley_view(self))

    def handle_trolley_resume(self, request: Request) -> tuple[int, dict[str, Any]]:
        try:
            trolley_module.move_resume(self.trolley, self.hoist, self.store)
            hoist_module.resume_release(self.hoist, self.feedback, self.store)
        except HookNotLowered:
            return 409, fail("hook not lowered")
        except FeedbackNotConfirmed:
            return 409, fail("feedback not confirmed")
        return 200, ok(
            {"trolley": views.trolley_view(self), "hoist": views.hoist_view(self)}
        )

    def handle_hoist_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.hoist_view(self))

    def handle_hoist_payout(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        self.drum.pay_out(_number(request.body.get("length")))
        return 200, ok(views.hoist_view(self))

    def handle_hoist_feedback(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        self.feedback.confirm(_number(request.body.get("speed")))
        try:
            slew_module.persist_feedback(self.feedback, self.store)
        except StoreError as exc:
            return 500, fail(str(exc))
        self.audit.record(self.hoist.id, "speed-feedback", "confirmed")
        return 200, ok(views.hoist_view(self))

    def handle_hoist_release(self, request: Request) -> tuple[int, dict[str, Any]]:
        try:
            hoist_module.release_brake(self.hoist, self.feedback)
        except FeedbackNotConfirmed:
            return 409, fail("feedback not confirmed")
        try:
            slew_module.persist_feedback(self.feedback, self.store)
        except StoreError as exc:
            LOGGER.debug("best-effort feedback persist skipped after release: %s", exc)
        return 200, ok(views.hoist_view(self))

    def handle_hoist_stop(self, request: Request) -> tuple[int, dict[str, Any]]:
        hoist_module.stop(self.hoist, self.audit)
        hoist_module.engage_brake(self.hoist)
        return 200, ok(views.hoist_view(self))

    # ------------------------------------------------------------------ limit

    def handle_limit_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.limit_view(self))

    def handle_limit_check(self, request: Request) -> tuple[int, dict[str, Any]]:
        positions = fleet.current_positions(self.cluster)
        envelopes: list[limit_module.Envelope] = []
        slew_point = positions.get("crane-1")
        trolley_point = positions.get("crane-2")
        if slew_point is not None:
            envelopes.append(
                limit_module.position_envelope(
                    limit_module.AXIS_SLEW, limit_module.slew_position(slew_point.x)
                )
            )
        if trolley_point is not None:
            envelopes.append(
                limit_module.position_envelope(
                    limit_module.AXIS_TROLLEY, limit_module.trolley_position(trolley_point.y)
                )
            )
        exceeded = limit_module.enforce_envelopes(self.limit, envelopes)
        arbitration: limit_module.Arbitration | None = None
        if exceeded:
            arbitration = limit_module.arbitrate(
                self.limit, self.slew, self.trolley, self.hoist
            )
            self.audit.record("system", "limit-enforce", "axes=" + ",".join(exceeded))
            for axis in exceeded:
                self._raise_alarm(
                    location=f"{self.slew.id}/limit/{axis}",
                    kind=f"limit-{axis}",
                    severity=alarms_module.SEVERITY_MINOR,
                    title=f"{limit_module.AXIS_NAMES.get(axis, axis)}限位越限",
                    message="travel envelope exceeded",
                )
        return 200, ok(
            {
                "exceeded": exceeded,
                "margins": limit_module.envelope_margins(envelopes),
                "limit": views.limit_view(self),
                "arbitration": arbitration.to_dict() if arbitration else None,
            }
        )

    # ---------------------------------------------------------------- console

    def handle_config(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(site_config.config_view(self.config, self.cluster))

    def handle_config_save(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        try:
            updated = SiteConfig.from_dict(request.body)
        except ConfigError as exc:
            return 400, fail(str(exc))
        self.config = updated
        site_config.save_config(self.store, updated)
        self.audit.record("system", "config-save", updated.site_name)
        return 200, ok(site_config.config_view(updated, self.cluster))

    def handle_audit(self, request: Request) -> tuple[int, dict[str, Any]]:
        crane_id = request.query_first("crane") or ""
        limit = _positive_int(request.query_first("limit"), 50)
        actions = request.query_all("action")
        if actions:
            entries = self.audit.filter_by_actions(actions)[:limit]
        else:
            entries = self.audit.query(crane_id, limit)
        return 200, ok([entry.to_dict() for entry in entries])

    # ------------------------------------------------------------------ alarms

    def handle_alarms(self, request: Request) -> tuple[int, dict[str, Any]]:
        status = request.query_first("status") or ""
        location = request.query_first("location") or ""
        kind = request.query_first("kind") or ""
        limit = _positive_int(request.query_first("limit"), 100)
        incidents = self.alarms.list(status=status, location=location, kind=kind, limit=limit)
        return 200, ok(views.alarms_view(self, incidents))

    def handle_alarm_acknowledge(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        incident_id = str(request.body.get("id") or "")
        if not incident_id:
            return 400, fail("id required")
        operator = str(request.body.get("operator") or "operator")
        try:
            incident = self.alarms.acknowledge(incident_id, operator)
        except AlarmNotFound:
            return 404, fail("alarm not found")
        except AlarmStateError as exc:
            return 409, fail(str(exc))
        self._persist_alarms()
        self.audit.record(operator, "alarm-acknowledge", f"{incident.location} {incident.kind}")
        return 200, ok(incident.to_dict())

    def handle_alarm_resolve(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        incident_id = str(request.body.get("id") or "")
        if not incident_id:
            return 400, fail("id required")
        operator = str(request.body.get("operator") or "operator")
        try:
            incident = self.alarms.resolve(incident_id, operator)
        except AlarmNotFound:
            return 404, fail("alarm not found")
        except AlarmStateError as exc:
            return 409, fail(str(exc))
        self._persist_alarms()
        self.audit.record(operator, "alarm-resolve", f"{incident.location} {incident.kind}")
        return 200, ok(incident.to_dict())

    def handle_alarm_clear(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        operator = str(request.body.get("operator") or "operator")
        location = str(request.body.get("location") or "")
        kind = str(request.body.get("kind") or "")
        if not location and not kind:
            return 400, fail("location or kind required")
        closed = self.alarms.resolve_batch(
            operator,
            location=location,
            kind=kind,
            include_acknowledged=bool(request.body.get("include_acknowledged", True)),
        )
        self._persist_alarms()
        for incident in closed:
            self.audit.record(operator, "alarm-resolve", f"{incident.location} {incident.kind}")
        return 200, ok({"resolved": len(closed)})

    def _raise_alarm(
        self, location: str, kind: str, severity: str, title: str, message: str
    ) -> None:
        self.alarms.report(
            location=location,
            kind=kind,
            severity=severity,
            title=title,
            message=message,
        )
        self._persist_alarms()

    def _auto_clear_alarms(self, location: str, kind: str) -> None:
        closed = self.alarms.auto_resolve(location, kind)
        if closed:
            self._persist_alarms()
            for incident in closed:
                self.audit.record(
                    "system", "alarm-auto-resolve", f"{incident.location} {incident.kind}"
                )

    def _persist_alarms(self) -> None:
        try:
            alarms_module.save_board(self.store, self.alarms)
        except StoreError as exc:
            LOGGER.debug("best-effort alarm persist skipped: %s", exc)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _positive_int(value: str | None, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        parsed = int(value)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def _parse_points(payload: Any) -> dict[str, Any] | None:
    from .geometry import Point

    if not isinstance(payload, dict):
        return None
    points: dict[str, Point] = {}
    for crane_id, raw in payload.items():
        if not isinstance(raw, dict):
            return None
        try:
            points[str(crane_id)] = Point(float(raw["x"]), float(raw["y"]))
        except (KeyError, TypeError, ValueError):
            return None
    return points
