"""Persistent manual positions and connection-bound administrative sessions."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import math
import logging
import re
import secrets
import time
import unicodedata

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE
from homeassistant.core import Context, CoreState, callback, valid_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, MAX_HISTORY, MAX_PERSONS, MAX_SCENARIOS, MAX_SCENARIO_POINTS, PIN_MAX_ATTEMPTS, PIN_WINDOW_SECONDS,
    SCENARIO_RETRY_SECONDS, SCENARIO_STORAGE_ATTEMPTS, SESSION_SECONDS, STORAGE_VERSION,
)
from .security import hash_pin, valid_pin, verify_pin
from .gateway_bridge import GatewayBridge, GatewayError
from .schedule import SourceSchedule, empty_schedule, switch_state, validate_schedule, MAX_RETURNS

_LOGGER = logging.getLogger(__name__)
_REFRESH_LOGGER = logging.getLogger(f"{__name__}.location_refresh")
_REFRESH_LOGGER.setLevel(logging.INFO)
LOCATION_REPORT_TIMEOUT = 60
_UNSET = object()


def scheduled_return_at(hass, value):
    """Resolve one unambiguous local wall time in HA's configured timezone."""
    if (not isinstance(value, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", value)):
        raise LocationError("invalid_return_time")
    try:
        local = datetime.fromisoformat(value)
        zone = dt_util.get_time_zone(hass.config.time_zone)
        if zone is None:
            raise ValueError
        candidates = {
            local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            for fold in (0, 1)
            if local.replace(tzinfo=zone, fold=fold).astimezone(UTC).astimezone(zone).replace(tzinfo=None) == local
        }
        if len(candidates) != 1:
            raise ValueError
        instant = candidates.pop()
        if instant <= datetime.now(UTC):
            raise ValueError
        return int(instant.timestamp())
    except (ValueError, OverflowError, TypeError):
        raise LocationError("invalid_return_time") from None


def scenario_day(value: object) -> str:
    """Require a calendar day even when a saved draft has no points yet."""
    if not isinstance(value, str):
        raise LocationError("invalid_input")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise LocationError("invalid_input") from None
    return value


def stop_label(value: object) -> str:
    """Validate a planned stop without adding metadata to history/current."""
    if (not isinstance(value, str) or not value.strip() or len(value) > 300
            or any(unicodedata.category(char).startswith("C") for char in value)):
        raise LocationError("invalid_input")
    return value


def scenario_routing(value: object) -> dict | None:
    """Validate optional settings for the next route calculation."""
    if value is None:
        return None
    if (not isinstance(value, dict)
            or set(value) not in ({"mode", "interval_minutes"}, {"mode", "interval_minutes", "interval_max_minutes"})
            or not isinstance(value["mode"], str) or value["mode"] not in {"walk", "drive", "bicycle", "transit"}
            or type(value["interval_minutes"]) is not int or not 1 <= value["interval_minutes"] <= 1440
            or ("interval_max_minutes" in value and (type(value["interval_max_minutes"]) is not int
                or not value["interval_minutes"] <= value["interval_max_minutes"] <= 1440))):
        raise LocationError("invalid_input")
    return deepcopy(value)


def scenario_points(hass, points: object, *, check_day=True, day=None, allow_empty=False) -> list[dict]:
    """Normalize a one-day plan in the configured Home Assistant timezone."""
    if not isinstance(points, list) or not (0 if allow_empty else 1) <= len(points) <= MAX_SCENARIO_POINTS:
        raise LocationError("invalid_input")
    zone = dt_util.get_time_zone(hass.config.time_zone) if check_day else None
    if check_day and zone is None:
        raise LocationError("invalid_input")
    result, instants, days = [], set(), set()
    for point in points:
        if isinstance(point, dict) and ("stop" in point or "name" in point):
            normalized = coordinates({key: value for key, value in point.items() if key not in {"stop", "name"}})
            if "stop" in point:
                normalized["stop"] = stop_label(point["stop"])
            if "name" in point:
                if not isinstance(point["name"], str) or len(point["name"]) > 300 or any(
                    unicodedata.category(char).startswith("C") for char in point["name"]
                ):
                    raise LocationError("invalid_input")
                if point["name"].strip():
                    normalized["name"] = point["name"].strip()
        else:
            normalized = coordinates(point)
        instant = timestamp(point.get("timestamp"))
        if instant in instants:
            raise LocationError("invalid_input")
        instants.add(instant)
        if check_day:
            try:
                days.add(instant.astimezone(zone).date().isoformat())
            except (ValueError, OverflowError):
                raise LocationError("invalid_input") from None
        result.append(normalized | {"timestamp": instant.isoformat()})
    if check_day and (len(days) > 1 or (days and day is not None and days != {day})):
        raise LocationError("invalid_input")
    return sorted(result, key=lambda point: point["timestamp"])


def validate_running_intervals(scenarios: dict):
    """Running plans of one person reserve disjoint closed time intervals."""
    running = sorted(
        (scenario["entity_id"], scenario["points"][0]["timestamp"], scenario["points"][-1]["timestamp"])
        for scenario in scenarios.values() if scenario["status"] == "running"
    )
    previous_person, previous_end = None, None
    for entity_id, start, end in running:
        if entity_id == previous_person and start <= previous_end:
            raise LocationError("scenario_overlap")
        previous_person, previous_end = entity_id, end


def validate_scenarios(hass, scenarios: object) -> dict:
    """Validate persisted plans and migrate the previous one-plan-per-person shape."""
    if not isinstance(scenarios, dict) or len(scenarios) > MAX_SCENARIOS:
        raise LocationError("invalid_input")
    legacy_fields = {"name", "points", "status", "cursor", "reason"}
    new_fields = legacy_fields | {"entity_id", "day"}
    legacy = bool(scenarios) and all(
        isinstance(value, dict) and set(value) == legacy_fields for value in scenarios.values()
    )
    result = {}
    for key, original in scenarios.items():
        if not isinstance(original, dict):
            raise LocationError("invalid_input")
        scenario = deepcopy(original)
        if legacy:
            zone = dt_util.get_time_zone(hass.config.time_zone)
            if (
                zone is None or not isinstance(scenario["points"], list) or not scenario["points"]
                or not isinstance(scenario["points"][0], dict)
            ):
                raise LocationError("invalid_input")
            scenario["entity_id"] = key
            scenario["day"] = timestamp(scenario["points"][0].get("timestamp")).astimezone(zone).date().isoformat()
            key = f"legacy:{key}"
        if (
            not isinstance(key, str) or not 1 <= len(key) <= 120
            or set(scenario) - {"routing", "auto_custom", "auto_ha", "run_start_cursor", "run_id",
                               "switch_cursor", "switch_state"} != new_fields
            or not isinstance(scenario["entity_id"], str) or not scenario["entity_id"].startswith("person.")
            or not isinstance(scenario["name"], str) or not 1 <= len(scenario["name"].strip()) <= 80
            or scenario["status"] not in {"draft", "running", "stopped", "completed"}
            or type(scenario["cursor"]) is not int
            or not isinstance(scenario["reason"], str) or len(scenario["reason"]) > 80
        ):
            raise LocationError("invalid_input")
        scenario_day(scenario["day"])
        for field in ("auto_custom", "auto_ha"):
            if field in scenario and type(scenario[field]) is not bool:
                raise LocationError("invalid_input")
        for field in ("run_start_cursor", "switch_cursor"):
            if field in scenario and (type(scenario[field]) is not int
                                      or not 0 <= scenario[field] <= scenario["cursor"]):
                raise LocationError("invalid_input")
        if "run_id" in scenario and scenario["run_id"] is not None and (
            not isinstance(scenario["run_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", scenario["run_id"])
        ):
            raise LocationError("invalid_input")
        if "switch_state" in scenario:
            state = scenario["switch_state"]
            if (not isinstance(state, dict) or set(state) != {"custom", "normal", "reason"}
                    or state["custom"] not in {"pending", "done", "off"}
                    or state["normal"] not in {"pending", "attempting", "started", "done", "skipped", "off"}
                    or not isinstance(state["reason"], str) or len(state["reason"]) > 80
                    or (state["custom"] == "pending" and not scenario.get("run_id"))):
                raise LocationError("invalid_input")
        if "routing" in scenario:
            routing = scenario_routing(scenario["routing"])
            if routing is None:
                del scenario["routing"]
            else:
                scenario["routing"] = routing
        # A timezone change must not invalidate a plan accepted in the old zone.
        points = scenario_points(hass, scenario["points"], check_day=False, allow_empty=True)
        cursor = scenario["cursor"]
        if (
            not 0 <= cursor <= len(points)
            or (scenario["status"] == "draft" and cursor != 0)
            or (scenario["status"] == "running" and cursor == len(points))
            or (scenario["status"] == "completed" and (not points or cursor != len(points)))
            or points != scenario["points"]
        ):
            raise LocationError("invalid_input")
        result[key] = scenario
    validate_running_intervals(result)
    return result


class LocationError(Exception):
    """A safe WebSocket error containing no coordinates or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def timestamp(value: object) -> datetime:
    """Parse an ISO timestamp with an explicit UTC offset."""
    if not isinstance(value, str):
        raise LocationError("invalid_input")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise LocationError("invalid_input") from None


def coordinates(value: object) -> dict:
    """Validate a geographic point without trusting its client timestamp."""
    if not isinstance(value, dict) or set(value) - {"latitude", "longitude", "timestamp"}:
        raise LocationError("invalid_input")
    result = {}
    for key, limit in (("latitude", 90), ("longitude", 180)):
        number = value.get(key)
        # Check bounds before isfinite so huge JSON integers cannot overflow float.
        if type(number) not in (int, float) or not -limit <= number <= limit or not math.isfinite(number):
            raise LocationError("invalid_input")
        result[key] = float(number)
    return result


def validate_quick_save(hass, value: object, *, check_entities=True) -> dict:
    """Keep aliases separate from position/history and expose one chosen target."""
    if (not isinstance(value, dict) or set(value) != {"nicknames", "entity_id"}
            or not isinstance(value["nicknames"], dict) or len(value["nicknames"]) > MAX_PERSONS):
        raise LocationError("invalid_input")
    nicknames = {}
    for entity_id, nickname in value["nicknames"].items():
        if (not isinstance(entity_id, str) or not entity_id.startswith("person.")
                or not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= 40
                or any(unicodedata.category(char).startswith("C") for char in nickname)):
            raise LocationError("invalid_input")
        nicknames[entity_id] = nickname.strip()
    target = value["entity_id"]
    if target is not None and (
        not isinstance(target, str) or not target.startswith("person.") or target not in nicknames
        or (check_entities and hass.states.get(target) is None)
    ):
        raise LocationError("invalid_input")
    return {"nicknames": nicknames, "entity_id": target}


def validate_persons(hass, persons: object, previous: dict, now: datetime, *, check_entities=True) -> dict:
    """Normalize a complete snapshot and keep current independent of history."""
    if not isinstance(persons, dict) or len(persons) > MAX_PERSONS:
        raise LocationError("invalid_input")
    result = {}
    for entity_id, settings in persons.items():
        if (
            not isinstance(entity_id, str) or not entity_id.startswith("person.")
            or (check_entities and hass.states.get(entity_id) is None) or not isinstance(settings, dict)
            or set(settings) not in (
                {"enabled", "current", "history"},
                {"enabled", "current", "history", "current_history_timestamp"},
                {"enabled", "current", "history", "record_current_now"},
                {"enabled", "current", "history", "record_history_now"},
            )
            # Persisted snapshots cannot request a new point during reload.
            or ("record_current_now" in settings and (settings["record_current_now"] is not True or not check_entities))
            or ("record_history_now" in settings and not check_entities)
            or type(settings["enabled"]) is not bool
            or not isinstance(settings["history"], list)
            or len(settings["history"]) > MAX_HISTORY
        ):
            raise LocationError("invalid_input")
        current = coordinates(settings["current"])
        history = []
        seen = set()
        for point in settings["history"]:
            normalized = coordinates(point)
            instant = timestamp(point.get("timestamp"))
            if instant > now or instant in seen:
                raise LocationError("invalid_input")
            seen.add(instant)
            history.append(normalized | {"timestamp": instant.isoformat()})
        old_current = previous.get(entity_id, {}).get("current")
        if "record_history_now" in settings:
            point = settings["record_history_now"]
            if (not isinstance(point, dict) or set(point) != {"latitude", "longitude"}
                    or old_current is None or current != coordinates(old_current)
                    or now in seen or len(history) >= MAX_HISTORY):
                raise LocationError("invalid_input")
            # Request-only marker: HA supplies the time without changing current.
            history.append(coordinates(point) | {"timestamp": now.isoformat()})
        if "current_history_timestamp" in settings:
            current_at = timestamp(settings["current_history_timestamp"]).isoformat()
            selected = next((point for point in history if point["timestamp"] == current_at), None)
            if selected is None or any(current[k] != selected[k] for k in current):
                raise LocationError("invalid_input")
            # Consume the draft marker; only the validated historical point persists.
            current = deepcopy(selected)
        elif (
            not settings.get("record_current_now") and old_current is not None
            and all(current[k] == old_current[k] for k in current)
        ):
            current = deepcopy(old_current)
        else:
            if now in seen or len(history) >= MAX_HISTORY:
                raise LocationError("invalid_input")
            current["timestamp"] = now.isoformat()
            history.append(deepcopy(current))
        history.sort(key=lambda point: point["timestamp"])
        result[entity_id] = {"enabled": settings["enabled"], "current": current, "history": history}
    return result


def validate_normal_intent(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"id", "person_id", "tracker_id", "phone_entry_id"}
            or not isinstance(value["id"], str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value["id"]) is None
            or any(not isinstance(value[key], str) or not valid_entity_id(value[key]) for key in ("person_id", "tracker_id"))
            or not value["person_id"].startswith("person.") or not value["tracker_id"].startswith("device_tracker.")
            or not isinstance(value["phone_entry_id"], str) or not 1 <= len(value["phone_entry_id"]) <= 128):
        raise LocationError("invalid_input")
    return deepcopy(value)


@dataclass
class Session:
    user_id: str
    connection: object
    expires: float


class ManualLocation:
    """Own only this integration's Store and local access controls."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.store = Store(hass, STORAGE_VERSION, DOMAIN, private=True, atomic_writes=True, serialize_in_event_loop=False)
        self.data = {"revision": 0, "enabled": True, "persons": {}, "scenarios": {},
                     "quick_save": {"nicknames": {}, "entity_id": None}, "activity": {}}
        self.sessions: dict[str, Session] = {}
        self.failures: dict[str, list[float]] = {}
        self.blocked_until: dict[str, float] = {}
        self._auth_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self.loaded = True
        self._started = False
        self._timer_cancel = None
        self._state_cancel = None
        self._startup_cancel = None
        self._retry_at = None
        self._storage_attempts = 0
        self._suspended = set()
        self._geocoder = None
        self._phone_activity = None
        self._refresh_person_until: dict[str, float] = {}
        self._refresh_phone_until: dict[tuple[str, str], float] = {}
        self._refresh_cleanups = {}
        self.gateway = GatewayBridge.from_entry(hass, entry)
        self._location_history = None
        self._gateway_retry_cancel = None
        self._gateway_applied = None
        self.source_schedule = SourceSchedule(self)

    @property
    def location_history(self):
        if self._location_history is None:
            from .location_history import LocationHistory

            self._location_history = LocationHistory(self)
        return self._location_history

    def _gateway_current(self, snapshot):
        if self.gateway is None or not snapshot["enabled"]:
            return None
        person = snapshot["persons"].get(self.gateway.person_id)
        return person["current"] if person and person["enabled"] else None

    def _normal_identity(self):
        if self.gateway is None:
            raise LocationError("history_unavailable")
        tracker = self.location_history._tracker()
        entry = er.async_get(self.hass).async_get(tracker)
        if entry is None or not entry.config_entry_id:
            raise LocationError("history_unavailable")
        return {"person_id": self.gateway.person_id, "tracker_id": tracker, "phone_entry_id": entry.config_entry_id}

    def _new_normal_intent(self):
        return {"id": secrets.token_urlsafe(32), **self._normal_identity()}

    def _normal_intent_valid(self):
        intent = self.data.get("normal_intent")
        if (intent is None or self.gateway is None or not self.data["enabled"]
                or self.data["persons"].get(self.gateway.person_id, {}).get("enabled", True)):
            return False
        try:
            return all(intent[key] == value for key, value in self._normal_identity().items())
        except LocationError:
            return False

    async def async_gateway_control_state(self):
        try:
            identity = self._normal_identity()
        except LocationError:
            identity = {"person_id": self.gateway.person_id, "tracker_id": None, "phone_entry_id": None}
        desired = self._normal_intent_valid()
        ready = self.loaded and identity["tracker_id"] is not None and await self.location_history.async_ready()
        ready = ready and not self.location_history.correction_active and not self.data.get("return_schedule", {}).get("journal")
        return {"session": self.gateway.session,
                "core_boot": self.hass.data.setdefault(f"{DOMAIN}_core_boot", secrets.token_urlsafe(32)),
                "normal_intent_id": self.data["normal_intent"]["id"] if desired else None,
                "normal_desired": desired, "resume_ready": bool(ready), **identity}

    async def _async_gateway_resume(self, status):
        """Restore only the matching live Gateway entitlement; never create consent."""
        if not self._normal_intent_valid() or not status.get("resume_supported"):
            return False
        intent = deepcopy(self.data["normal_intent"])
        if status.get("resume_intent_id") != intent["id"]:
            return False
        if (status.get("history_correction_pending") or self.location_history.correction_active
                or any(status.get(key) for key in ("upstream_uncertain", "recovery_retryable", "replay_required"))
                or (status.get("handoff") or {}).get("state") in {"prepared", "applied"}):
            self._schedule_gateway_retry()
            return True
        if (status.get("scheduled_return") or {}).get("state") in {"due", "applied"}:
            await self._async_gateway_return_reconcile(status)
            return False
        if any(item["at"] <= datetime.now(UTC).timestamp() for item in self.source_schedule.events()):
            await self.source_schedule.tick()
            return False
        if not await self.location_history.async_ready():
            raise GatewayError("resume history unavailable")
        active = self.location_history.grants.get(self.location_history.active_grant)
        if status.get("suspended") and active and active["session"] == self.gateway.session:
            self.location_history.close_confirmed()
            self.gateway.invalidate()
        session = self.gateway.session
        self.gateway.ready, self.gateway.pending = True, False
        status = await self.source_schedule.sync(status, opening=True)
        if not self._normal_intent_valid() or self.data["normal_intent"] != intent or self.gateway.session != session:
            raise GatewayError("resume intent changed")
        active = self.location_history.grants.get(self.location_history.active_grant)
        if (not status.get("suspended") and status["mode"] in {"normal", "warmup"}
                and active and active["session"] == session and active.get("intent_id") == intent["id"]):
            return True
        operation = self.gateway.key(self.data["revision"], "resume", {"intent_id": intent["id"], "session": session})
        await self.location_history.begin_normal(operation, intent_id=intent["id"])
        plan = status.get("scheduled_return")
        try:
            status = await self.gateway.resume(operation, intent["id"], expected_id=plan["id"] if plan else None)
            if not self._normal_intent_valid() or self.data["normal_intent"] != intent or self.gateway.session != session:
                raise GatewayError("resume intent changed")
        except BaseException:
            self.location_history.close_confirmed()
            self.gateway.ready = False
            raise
        self._gateway_applied = None
        for key, route in self.data["scenarios"].items():
            if route["status"] == "completed" and route.get("switch_state", {}).get("normal") in {"attempting", "started"}:
                self.source_schedule.normal_session[key] = self.gateway.session
        return True

    def _gateway_failed(self):
        self.location_history.close_started()
        self.gateway.invalidate()
        self.gateway.pending = True
        self._schedule_gateway_retry()

    def _schedule_gateway_retry(self):
        if self.loaded and self._started and self._gateway_retry_cancel is None:
            self._gateway_retry_cancel = async_track_point_in_utc_time(
                self.hass, self._async_gateway_retry, datetime.now(UTC) + timedelta(seconds=5),
            )

    async def _async_gateway_close(self, revision):
        self.location_history.close_started()
        try:
            await self.gateway.close(self.gateway.key(revision, "close"))
            self.location_history.close_confirmed()
            self._gateway_applied = None
        except GatewayError:
            self._gateway_failed()
        except asyncio.CancelledError:
            self._gateway_failed()
            raise

    async def _async_gateway_sync(self):
        """The Store is the latest own target; never enqueue older positions."""
        if self._location_history is not None and self._location_history.correction_active:
            return
        if self.gateway.handoff_supported or self.gateway.resume_supported or not self.gateway.ready:
            try:
                status = await self.gateway.status()
                if await self._async_gateway_resume(status):
                    return
                if status.get("handoff_supported"):
                    preserve_normal = self._normal_intent_valid() and (status.get("scheduled_return") or {}).get("state") not in {"due", "applied"}
                    await self._async_gateway_handoff_reconcile(status, preserve_normal=preserve_normal)
            except GatewayError:
                self._gateway_failed()
                return
        current = self._gateway_current(self.data)
        if self.gateway.ready and not self.gateway.pending and self._gateway_applied == current:
            return
        if current is None:
            await self._async_gateway_close(self.data["revision"])
            return
        try:
            self.location_history.close_started()
            await self.gateway.custom(self.gateway.key(self.data["revision"], "custom", current), current)
            self.location_history.close_confirmed()
            self._gateway_applied = deepcopy(current)
        except GatewayError:
            self._gateway_failed()
        except asyncio.CancelledError:
            self._gateway_failed()
            raise

    async def _async_gateway_retry(self, _now):
        self._gateway_retry_cancel = None
        async with self._save_lock:
            if self.loaded:
                if self._location_history is not None and self._location_history.correction_active:
                    try:
                        await self._location_history.async_recover()
                    except Exception:
                        # A transient status failure must not strand the busy fence
                        # or revoke a healthy GPS grant while correction recovery waits.
                        self._schedule_gateway_retry()
                        return
                    if self._location_history.correction_active:
                        self._schedule_gateway_retry()
                        return
                try:
                    await self._async_gateway_sync()
                    await self.location_history.async_recover()
                    if self.gateway.return_supported:
                        await self._async_gateway_return_reconcile(await self.gateway.status())
                        await self.source_schedule.tick()
                except GatewayError:
                    self._gateway_failed()
                except Exception:
                    # A history-only failure does not require republishing an
                    # already confirmed own point (or creating new Recorder IDs).
                    if self.gateway.ready and not self.gateway.pending:
                        self._schedule_gateway_retry()
                    else:
                        self._gateway_failed()
                finally:
                    if self.gateway.return_supported or self.gateway.resume_supported:
                        self._schedule_gateway_retry()

    async def _async_gateway_return_reconcile(self, status):
        if status.get("history_correction_pending") or (self._location_history is not None and self._location_history.correction_active):
            return status
        """Called with _save_lock; persist Own before consuming the Gate latch."""
        preserve = self._normal_intent_valid() and (status.get("scheduled_return") or {}).get("state") not in {"due", "applied"}
        status = await self._async_gateway_handoff_reconcile(status, preserve_normal=preserve)
        plan = status.get("scheduled_return")
        if plan is None or plan["state"] not in {"due", "applied"}:
            return status
        self.location_history.close_started(cutoff=plan["at"])
        if status.get("handoff_supported"):
            # Expiry may be observed before the Gate's recovery task has
            # frozen its last admitted report. Drain/freeze before constructing
            # any snapshot from the previous Own current.
            latch = status.get("handoff")
            if latch is None or latch["state"] == "idle":
                await self.gateway.handoff(self.gateway.key(self.data["revision"], "return-handoff", plan["id"]),
                                           latch["id"] if latch else None)
                status = await self._async_gateway_handoff_reconcile(await self.gateway.status())
        # Exact-delete recovery owns the current Store hash until its journal
        # is cleaned. Never starve that recovery with a new source write.
        await self.location_history.async_recover()
        person = self.data["persons"].get(self.gateway.person_id)
        if not person or not person.get("current"):
            self._schedule_gateway_retry()
            return status
        if not person["enabled"]:
            snapshot = self._snapshot()
            snapshot["persons"][self.gateway.person_id]["enabled"] = True
            snapshot["revision"] += 1
            await self._commit(snapshot)
            self._reschedule()
        if not self.data["enabled"]:
            # Global off remains off, but the Gate still needs the actual own
            # target from Store before we acknowledge this return.
            current = self.data["persons"][self.gateway.person_id]["current"]
            await self.gateway.custom(self.gateway.key(self.data["revision"], "return-own", current), current)
            self._gateway_applied = None
        else:
            await self._async_gateway_sync()
        current_status = await self.gateway.status()
        current_plan = current_status.get("scheduled_return")
        if (current_plan and current_plan["id"] == plan["id"] and current_plan["state"] == "applied"
                and current_status["mode"] == "custom" and current_status["gps_blocked"]
                and not any(current_status[name] for name in ("pending", "replay_required", "upstream_uncertain", "recovery_retryable"))):
            self.location_history.close_confirmed()
            return await self.gateway.acknowledge_return(
                self.gateway.key(self.data["revision"], "return-ack", plan["id"]), plan["id"],
            )
        self._schedule_gateway_retry()
        return current_status

    async def _async_gateway_handoff_reconcile(self, status, *, preserve_normal=False):
        """Commit one frozen target before ACK; called only with _save_lock."""
        handoff = status.get("handoff")
        if not handoff or handoff["state"] == "idle":
            return status
        # Gate-initiated deadline/recovery handoffs also revoke an old Normal
        # consent. Its delayed request must not reopen GPS after our ACK.
        self.gateway.invalidate()
        self.location_history.close_started()
        await self.location_history.async_recover()
        result = await self.gateway.read_handoff()
        if result is None or result.get("id") != handoff["id"] or result.get("state") != "applied":
            raise GatewayError("handoff recovery pending")
        current = result["current"]
        person = self.data["persons"].get(self.gateway.person_id)
        if not person:
            raise GatewayError("handoff person unavailable")
        if person["current"] != current or person["enabled"] == preserve_normal:
            snapshot = self._snapshot()
            target = snapshot["persons"][self.gateway.person_id]
            target.update(current=deepcopy(current), enabled=not preserve_normal)
            if not preserve_normal:
                snapshot["normal_intent"] = None
            if not any(timestamp(point["timestamp"]) == timestamp(current["timestamp"]) for point in target["history"]):
                if len(target["history"]) >= MAX_HISTORY:
                    raise LocationError("history_too_large")
                target["history"].append(deepcopy(current))
                target["history"].sort(key=lambda point: timestamp(point["timestamp"]))
            snapshot["revision"] += 1
            await self.store.async_save(snapshot)
            if await self.store.async_load() != snapshot:
                raise LocationError("storage_error")
            self._require_loaded()
            self.data = snapshot
            self._reschedule()
        if any(timestamp(point["timestamp"]) == timestamp(current["timestamp"])
               and coordinates(point) != coordinates(current)
               for point in self.data["persons"][self.gateway.person_id]["history"]):
            # Own history keeps its existing timestamp-based edit/delete IDs.
            # Preserve a different tied point in the already supported HA axis,
            # bound to the actual verified Own publication and its native refs.
            await self.location_history.async_preserve_handoff(handoff["id"], current)
        # The Store write is complete before invalidating the old operation
        # pointer. Repeating after a crash only strengthens the Own fence.
        await self.location_history.async_own_current()
        status = await self.gateway.acknowledge_handoff(
            self.gateway.key(self.data["revision"], "handoff-ack", handoff["id"]), handoff["id"])
        self._gateway_applied = deepcopy(self._gateway_current(self.data))
        return status

    async def _async_gateway_return_preflight(self):
        if self._location_history is not None and self._location_history.correction_active:
            raise LocationError("history_recovering")
        if self.gateway is None:
            return None
        try:
            status = await self.gateway.status()
            return await self._async_gateway_return_reconcile(status) if status["scheduled_return_supported"] else None
        except GatewayError:
            self._gateway_failed()
            return None

    async def _async_gateway_target_guard(self, snapshot=None, *, start=False):
        """The armed Gate target cannot diverge from Store; called under _save_lock."""
        if self._location_history is not None and self._location_history.correction_active:
            raise LocationError("history_recovering")
        if self.gateway is None or not self.gateway.return_supported:
            return
        person_id = self.gateway.person_id
        if "return_schedule" in self.data:
            if start:
                return
            old = self.data["persons"].get(person_id, {}).get("current")
            new = snapshot["persons"].get(person_id, {}).get("current")
            if old == new:
                return
            # The durable queue owns future cutoffs. Close before disarming its
            # projection, so a point write can never create an unguarded GPS gap.
            await self._async_gateway_close(self.data["revision"])
            status = await self._async_gateway_return_reconcile(await self.gateway.status())
            await self.source_schedule.sync(status)
            snapshot["return_schedule"] = deepcopy(self.data["return_schedule"])
            snapshot["revision"] = max(snapshot["revision"], self.data["revision"] + 1)
            return
        if not start:
            def target(data):
                current = data["persons"].get(person_id, {}).get("current")
                return None if current is None else (current["latitude"], current["longitude"])
            starts_route = any(
                value["entity_id"] == person_id and value["status"] == "running"
                and self.data["scenarios"].get(key, {}).get("status") != "running"
                for key, value in snapshot["scenarios"].items()
            )
            if target(snapshot) == target(self.data) and not starts_route:
                return
        try:
            status = await self.gateway.status()
        except GatewayError:
            self._gateway_failed()
            raise LocationError("return_status_unavailable") from None
        if not status["scheduled_return_supported"]:
            raise LocationError("return_status_unavailable")
        plan = status["scheduled_return"]
        if plan and plan["state"] in {"armed", "due", "applied"}:
            raise LocationError("return_target_locked")

    async def async_gateway_return(self, connection, token, revision, expected_id, local_time):
        async with self._save_lock:
            self.require_session(connection, token)
            if "return_schedule" in self.data:
                raise LocationError("client_outdated")
            if self.gateway is None:
                raise LocationError("gateway_return_unavailable")
            try:
                status = await self._async_gateway_return_reconcile(await self.gateway.status())
                self.require_session(connection, token)
                self._require_revision(revision)
                plan = status.get("scheduled_return")
                if not status["scheduled_return_supported"]:
                    raise LocationError("gateway_return_unavailable")
                if expected_id != (plan["id"] if plan else None):
                    raise LocationError("return_changed")
                if plan and plan["state"] in {"due", "applied"}:
                    raise LocationError("gateway_return_recovering")
                at = None if local_time is None else scheduled_return_at(self.hass, local_time)
                if at is not None:
                    if not self.data["persons"].get(self.gateway.person_id, {}).get("current") or not status["has_custom_point"]:
                        raise LocationError("gateway_return_requires_custom")
                    if any(value["entity_id"] == self.gateway.person_id and value["status"] == "running"
                           for value in self.data["scenarios"].values()):
                        raise LocationError("return_route_running")
                    current = self.data["persons"][self.gateway.person_id]["current"]
                    if status["custom_point_digest"] != self.gateway.custom_point_digest(current):
                        raise LocationError("return_own_unconfirmed")
                key = self.gateway.key(revision, "return", {"expected_id": expected_id, "at": at})
                result = await self.gateway.configure_return(key, expected_id, at)
                self.require_session(connection, token)
                self._schedule_gateway_retry()
                return result
            except GatewayError:
                self._gateway_failed()
                raise LocationError("gateway_return_unconfirmed") from None
            except asyncio.CancelledError:
                self._gateway_failed()
                raise

    async def async_gateway_status(self, connection, token):
        self.require_session(connection, token)
        if self.gateway is None:
            return {"enabled": False}
        try:
            result = await self.gateway.status()
            result["current"] = await self.location_history.async_current()
            result["desired_source"] = "off" if not self.data["enabled"] else ("custom" if self.data["persons"].get(self.gateway.person_id, {}).get("enabled", True) else "normal")
            result["resume_available"] = bool(self._normal_intent_valid() and result.get("resume_intent_id") == self.data["normal_intent"]["id"])
            result.pop("resume_intent_id", None)
            self.require_session(connection, token)
            return self.source_schedule.status(result)
        except GatewayError:
            self.require_session(connection, token)
            self._gateway_failed()
            return self.source_schedule.status({"enabled": True, "available": False, "person_id": self.gateway.person_id,
                    "mode": "blocked", "pending": True, "gps_blocked": False, "remaining_seconds": 0,
                    "desired_source": "off" if not self.data["enabled"] else ("custom" if self.data["persons"].get(self.gateway.person_id, {}).get("enabled", True) else "normal"),
                    "resume_supported": self.gateway.resume_supported, "resume_available": False,
                    "suspended": False, "suspend_reason": "transport_unavailable",
                    "has_custom_point": False, "replay_required": False, "upstream_uncertain": False,
                    "recovery_retryable": False, "last_custom_receipt": None, "last_phone_receipt": None,
                    "scheduled_return_supported": self.gateway.return_supported, "scheduled_return": None})

    async def async_gateway_return_save(self, connection, token, revision, schedule_revision, event_id, local_time):
        return await self._async_edit_return(connection, token, revision, schedule_revision, event_id, local_time)

    async def async_gateway_return_delete(self, connection, token, revision, schedule_revision, event_id):
        if not isinstance(event_id, str):
            raise LocationError("invalid_input")
        return await self._async_edit_return(connection, token, revision, schedule_revision, event_id, None)

    async def _async_edit_return(self, connection, token, revision, schedule_revision, event_id, local_time):
        async with self._save_lock:
            self.require_session(connection, token)
            self._require_revision(revision)
            if self.gateway is None:
                raise LocationError("gateway_return_unavailable")
            if type(schedule_revision) is not int or (event_id is not None and not isinstance(event_id, str)):
                raise LocationError("invalid_input")
            current = self.data.get("return_schedule", empty_schedule())
            if schedule_revision != current["revision"]:
                raise LocationError("return_changed")
            if event_id is not None and event_id.startswith("scenario:"):
                raise LocationError("return_route_managed")
            at = scheduled_return_at(self.hass, local_time) if local_time is not None else None
            try:
                status = await self._async_gateway_return_reconcile(await self.gateway.status())
                self.require_session(connection, token)
                if not status.get("scheduled_return_supported"):
                    raise LocationError("gateway_return_unavailable")
                if not status.get("has_custom_point"):
                    raise LocationError("gateway_return_requires_custom")
                await self.source_schedule.initialize(status)
                snapshot = deepcopy(self.data)
                schedule = snapshot["return_schedule"]
                if event_id is not None and event_id not in schedule["manual"]:
                    raise LocationError("return_changed")
                if at is None:
                    if event_id is None:
                        raise LocationError("invalid_input")
                    del schedule["manual"][event_id]
                else:
                    if event_id is None:
                        if len(schedule["manual"]) >= MAX_RETURNS:
                            raise LocationError("return_limit")
                        event_id = secrets.token_hex(16)
                    schedule["manual"][event_id] = {"at": at}
                schedule["revision"] += 1
                await self.source_schedule.persist(snapshot)
                await self.source_schedule.tick()
                self._reschedule()
                result = self.source_schedule.status(await self.gateway.status())
                result["snapshot"] = self._snapshot()
                return result
            except LocationError as error:
                if error.code == "storage_error":
                    self._gateway_failed()
                raise
            except GatewayError:
                self._gateway_failed()
                raise LocationError("gateway_return_unconfirmed") from None
            except (Exception, asyncio.CancelledError):
                self._gateway_failed()
                raise
            finally:
                self._reschedule()

    async def _async_cancel_route_warmup(self, previous, replacement):
        if (self.gateway is None or not previous
                or previous.get("switch_state", {}).get("normal") not in {"attempting", "started"}):
            return
        if replacement and replacement.get("auto_ha") and replacement.get("run_id") == previous.get("run_id"):
            return
        status = await self.gateway.status()
        suspended_intent = (status.get("suspended") and self._normal_intent_valid()
                            and status.get("resume_intent_id") == self.data["normal_intent"]["id"])
        if status["mode"] == "warmup" or suspended_intent:
            await self.source_schedule.enter_custom(status)
            if replacement is not None:
                replacement["switch_state"].update(normal="off" if not replacement.get("auto_ha") else "skipped",
                                                    reason="normal_cancelled")
            return True
        return False

    @property
    def phone_activity(self):
        if self._phone_activity is None:
            from .phone_activity import PhoneActivity

            self._phone_activity = PhoneActivity(self)
        return self._phone_activity

    @property
    def geocoder(self):
        """Initialize optional search only when an administrator requests it."""
        if self._geocoder is None:
            from .geocoding import PlaceGeocoder

            self._geocoder = PlaceGeocoder(self.hass, self.entry)
        return self._geocoder

    async def async_load(self):
        from .phone_activity import validate_activity

        stored = await self.store.async_load()
        if stored is None:
            if self.entry.data.get("storage_initialized"):
                raise LocationError("storage_error")
            await self.store.async_save(self.data)
            if await self.store.async_load() != self.data:
                raise LocationError("storage_error")
        else:
            if (
                not isinstance(stored, dict) or set(stored) - {"activity", "return_schedule", "normal_intent"} not in (
                    {"revision", "enabled", "persons"}, {"revision", "enabled", "persons", "scenarios"},
                    {"revision", "enabled", "persons", "quick_save"},
                    {"revision", "enabled", "persons", "scenarios", "quick_save"},
                )
                or type(stored["revision"]) is not int or stored["revision"] < 0
                or type(stored["enabled"]) is not bool or not isinstance(stored["persons"], dict)
            ):
                raise LocationError("storage_error")
            original = deepcopy(stored)
            now = datetime.now(UTC)
            try:
                for person in stored["persons"].values():
                    if timestamp(person["current"]["timestamp"]) > now:
                        raise LocationError("storage_error")
                stored["persons"] = validate_persons(
                    self.hass, stored["persons"], stored["persons"], now, check_entities=False
                )
                stored["scenarios"] = validate_scenarios(self.hass, stored.get("scenarios", {}))
                for scenario in stored["scenarios"].values():
                    if scenario["status"] == "running" and "run_start_cursor" not in scenario:
                        # An old executed prefix does not prove this restored run
                        # has started. Preserve waiting plans and deny old HA consent.
                        scenario.update(run_start_cursor=scenario["cursor"], auto_ha=False)
                if "return_schedule" in stored:
                    stored["return_schedule"] = validate_schedule(stored["return_schedule"])
                if "normal_intent" in stored:
                    stored["normal_intent"] = validate_normal_intent(stored["normal_intent"])
                stored["activity"] = validate_activity(self.hass, stored.get("activity", {}), check_entities=False)
                stored["quick_save"] = validate_quick_save(
                    self.hass, stored.get("quick_save", {"nicknames": {}, "entity_id": None}), check_entities=False,
                )
            except (LocationError, KeyError, TypeError, ValueError, OverflowError):
                raise LocationError("storage_error") from None
            if stored != original:
                try:
                    await self.store.async_save(stored)
                    if await self.store.async_load() != stored:
                        raise LocationError("storage_error")
                except Exception:
                    raise LocationError("storage_error") from None
            self.data = stored
        if not self.entry.data.get("storage_initialized"):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, "storage_initialized": True}
            )
        await self.phone_activity.async_load()

    def _require_loaded(self):
        if not self.loaded:
            raise LocationError("not_loaded")

    def require_session(self, connection, token):
        self._require_loaded()
        if connection.user is None or not connection.user.is_admin:
            raise LocationError("unauthorized")
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value.expires > now}
        session = self.sessions.get(token) if isinstance(token, str) else None
        if session is None or session.user_id != connection.user.id or session.connection is not connection:
            raise LocationError("pin_required")

    def forget_connection(self, connection):
        self.sessions = {key: value for key, value in self.sessions.items() if value.connection is not connection}

    async def async_unlock(self, connection, pin):
        async with self._auth_lock:
            self._require_loaded()
            if connection.user is None or not connection.user.is_admin:
                raise LocationError("unauthorized")
            user_id = connection.user.id
            now = time.monotonic()
            if self.blocked_until.get(user_id, 0) > now:
                raise LocationError("rate_limited")
            failures = [instant for instant in self.failures.get(user_id, []) if instant > now - PIN_WINDOW_SECONDS]
            correct = valid_pin(pin) and await self.hass.async_add_executor_job(verify_pin, pin, self.entry.data["pin_hash"])
            self._require_loaded()
            if not correct:
                failures.append(now)
                self.failures[user_id] = failures
                if len(failures) >= PIN_MAX_ATTEMPTS:
                    self.blocked_until[user_id] = now + PIN_WINDOW_SECONDS
                raise LocationError("invalid_pin")
            self.failures.pop(user_id, None)
            self.blocked_until.pop(user_id, None)
            self.forget_connection(connection)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = Session(user_id, connection, time.monotonic() + SESSION_SECONDS)
            connection.subscriptions[DOMAIN] = lambda: self.forget_connection(connection)
            return {"token": token, "expires_in": SESSION_SECONDS}

    def admin_get(self, connection, token):
        self.require_session(connection, token)
        return self._snapshot()

    def quick_save_info(self, connection):
        """Return only the chosen alias before PIN entry to an HA administrator."""
        self._require_loaded()
        if connection.user is None or not connection.user.is_admin:
            raise LocationError("unauthorized")
        settings = self.data["quick_save"]
        target = settings["entity_id"]
        nickname = settings["nicknames"].get(target, "")
        configured = bool(target and nickname and self.hass.states.get(target) is not None)
        return {"configured": configured, "nickname": nickname if configured else ""}

    async def async_refresh_location(self, connection, entity_id, *, wait_for_report=False):
        """Check the current view or request one assigned Companion phone update."""
        self._require_loaded()
        user = connection.user
        if user is None or not user.is_admin:
            raise LocationError("unauthorized")
        if not isinstance(entity_id, str) or not entity_id.startswith("person.") or not valid_entity_id(entity_id):
            raise LocationError("invalid_input")
        if type(wait_for_report) is not bool:
            raise LocationError("invalid_input")
        if not user.permissions.check_entity(entity_id, POLICY_READ):
            raise LocationError("unauthorized")
        state = self.hass.states.get(entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            raise LocationError("refresh_unavailable")
        now = time.monotonic()
        if self._refresh_person_until.get(entity_id, 0) > now:
            raise LocationError("refresh_limited")
        manual = self.data["persons"].get(entity_id)
        manual_active = self.data["enabled"] and manual and manual["enabled"]
        protected_gateway = self.gateway if self.gateway is not None and self.gateway.person_id == entity_id else None
        gateway_sequence = 0
        if protected_gateway is not None:
            try:
                gateway_status = await protected_gateway.status()
            except GatewayError:
                self._gateway_failed()
                raise LocationError("refresh_unavailable") from None
            if gateway_status["mode"] != "normal":
                self._refresh_person_until[entity_id] = now + 60
                return {"reported": False} if wait_for_report else {}
            receipt = gateway_status["last_phone_receipt"]
            gateway_sequence = receipt["sequence"] if receipt is not None else 0
        if manual_active:
            self._refresh_person_until[entity_id] = now + 60
            if not wait_for_report:
                return {}
        trackers = state.attributes.get("device_trackers")
        if not manual_active and (not isinstance(trackers, (list, tuple)) or not self.hass.services.has_service("notify", "send_message")):
            raise LocationError("refresh_unavailable")
        registry = er.async_get(self.hass)

        def notify_for(tracker_id):
            if not isinstance(tracker_id, str) or not tracker_id.startswith("device_tracker."):
                return None
            tracker = registry.async_get(tracker_id)
            if (tracker is None or tracker.platform != "mobile_app" or tracker.disabled_by is not None
                    or not tracker.device_id or not tracker.config_entry_id):
                return None
            config_entry = self.hass.config_entries.async_get_entry(tracker.config_entry_id)
            tracker_state = self.hass.states.get(tracker_id)
            if (config_entry is None or config_entry.domain != "mobile_app" or config_entry.disabled_by is not None
                    or tracker_state is None or tracker_state.state == STATE_UNAVAILABLE):
                return None
            targets = [
                entry.entity_id for entry in er.async_entries_for_config_entry(registry, tracker.config_entry_id)
                if entry.domain == "notify" and entry.platform == "mobile_app"
                and entry.device_id == tracker.device_id and entry.disabled_by is None
                and (loaded := self.hass.states.get(entry.entity_id)) is not None and loaded.state != STATE_UNAVAILABLE
            ]
            if len(targets) > 1:
                raise LocationError("refresh_unavailable")
            return (targets[0], (tracker.config_entry_id, tracker.device_id)) if targets else None

        def current_binding():
            self._require_loaded()
            if (connection.user is not user or not user.is_admin
                    or not user.permissions.check_entity(entity_id, POLICY_READ)):
                raise LocationError("unauthorized")
            current_state = self.hass.states.get(entity_id)
            if current_state is None or current_state.state == STATE_UNAVAILABLE:
                raise LocationError("refresh_unavailable")
            override = self.data["persons"].get(entity_id)
            if self.data["enabled"] and override and override["enabled"]:
                return None
            assigned = current_state.attributes.get("device_trackers")
            if not isinstance(assigned, (list, tuple)):
                raise LocationError("refresh_unavailable")
            current = current_state.attributes.get("source")
            binding = notify_for(current) if current in assigned else None
            if binding is None:
                targets = {candidate for tracker in assigned if (candidate := notify_for(tracker)) is not None}
                if len(targets) != 1:
                    raise LocationError("refresh_unavailable")
                binding = targets.pop()
            return binding

        resolved = current_binding()
        if resolved is not None:
            target, phone_key = resolved
            if self._refresh_phone_until.get(phone_key, 0) > now:
                raise LocationError("refresh_limited")
            # Reserve both limits before the first await, including failed sends.
            self._refresh_person_until[entity_id] = now + 60
            self._refresh_phone_until[phone_key] = now + 60

        operation_id = secrets.token_hex(4)
        def stage(name):
            try:
                _REFRESH_LOGGER.info("Location refresh %s: %s (%.1fs)", operation_id, name, time.monotonic() - now)
            except Exception:
                # Diagnostics must not break delivery or escape a native callback.
                pass

        report = self.hass.loop.create_future() if wait_for_report else None
        unsubscribe = None
        gateway_poll = None
        task = asyncio.current_task()
        def cleanup():
            nonlocal unsubscribe
            if unsubscribe is not None:
                unsubscribe()
                unsubscribe = None
            if report is not None and not report.done():
                report.cancel()
            if gateway_poll is not None:
                gateway_poll.cancel()

        if wait_for_report:
            self._refresh_cleanups[task] = cleanup
            if resolved is not None and protected_gateway is None:
                @callback
                def receive_report(_data):
                    try:
                        if report.done():
                            return
                        binding = current_binding()
                        if binding is None or binding[1] != phone_key:
                            return
                        # The native webhook is a receipt, even if GPS is unchanged.
                        stage("report_received")
                        report.set_result(True)
                    except LocationError:
                        return
                    except Exception:
                        # HA dispatcher otherwise includes its arguments (GPS) in errors.
                        stage("report_check_failed")

                # Native Companion protocol; importing mobile_app loads unrelated integrations.
                unsubscribe = async_dispatcher_connect(
                    self.hass, f"mobile_app_location_update_{phone_key[0]}", receive_report,
                )
            elif resolved is not None:
                async def wait_gateway_report():
                    try:
                        while not report.done():
                            status = await protected_gateway.status()
                            binding = current_binding()
                            if binding is None or binding[1] != phone_key:
                                raise LocationError("refresh_unavailable")
                            receipt = status["last_phone_receipt"]
                            # A correlated phone RPC is temporarily uncertain until
                            # its native response closes. Keep the existing bounded
                            # wait; neither that transient status nor an own receipt
                            # is evidence of a completed phone report.
                            if status["mode"] == "normal" and receipt is not None and receipt["sequence"] > gateway_sequence:
                                report.set_result(True)
                                return
                            await asyncio.sleep(0.25)
                    except GatewayError:
                        self._gateway_failed()
                        if not report.done():
                            report.set_exception(LocationError("refresh_unavailable"))
                    except LocationError:
                        if not report.done():
                            report.set_exception(LocationError("refresh_unavailable"))

                gateway_poll = self.hass.async_create_task(wait_gateway_report(), "Manual Location phone receipt")
        try:
            async with asyncio.timeout(LOCATION_REPORT_TIMEOUT if wait_for_report else 12):
                if resolved is not None:
                    stage("sending")
                    await self.hass.services.async_call(
                        "notify", "send_message", {"message": "request_location_update"},
                        target={"entity_id": target}, blocking=True, context=Context(user_id=user.id),
                    )
                    stage("accepted")
                else:
                    stage("checking")
                if not wait_for_report:
                    return {}
                await report
                binding = current_binding()
                if binding is None or binding[1] != phone_key:
                    raise LocationError("refresh_unavailable")
                return {"reported": True}
        except TimeoutError:
            stage("no_report")
            if not wait_for_report:
                raise LocationError("refresh_failed") from None
            return {"reported": False}
        except asyncio.CancelledError:
            stage("cancelled")
            raise
        except LocationError:
            stage("access_or_assignment_changed")
            raise
        except Exception:
            stage("send_failed")
            raise LocationError("refresh_failed") from None
        finally:
            cleanup()
            self._refresh_cleanups.pop(task, None)

    def _snapshot(self):
        snapshot = deepcopy(self.data)
        # A failed disk cannot persist a stop. Report the local safety suspension
        # without pretending the durable cursor or snapshot has advanced.
        for scenario_id in self._suspended:
            if scenario := snapshot["scenarios"].get(scenario_id):
                scenario.update(status="stopped", reason="storage_error")
        return snapshot

    def _require_revision(self, revision):
        if self._location_history is not None and self._location_history.correction_active:
            raise LocationError("history_recovering")
        if type(revision) is not int:
            raise LocationError("invalid_input")
        if revision != self.data["revision"]:
            raise LocationError("revision_conflict")

    async def _commit(self, snapshot, *, gateway_normal=False):
        """Commit history/current/cursors together and confirm actual persistence."""
        self._require_loaded()
        if self._location_history is not None and self._location_history.correction_active:
            raise LocationError("history_recovering")
        if "normal_intent" in self.data:
            snapshot["normal_intent"] = deepcopy(self.data["normal_intent"])
        if self.gateway is not None:
            if gateway_normal:
                snapshot["normal_intent"] = self._new_normal_intent() if self.gateway.resume_supported else None
            elif not snapshot["enabled"] or snapshot["persons"].get(self.gateway.person_id, {}).get("enabled", True):
                snapshot["normal_intent"] = None
            else:
                snapshot["normal_intent"] = deepcopy(self.data.get("normal_intent"))
        await self._async_gateway_target_guard(snapshot)
        if (self._location_history is not None
                and self._location_history.db is not None and self._location_history.db.pending):
            raise LocationError("history_recovering")
        # Any later successful write makes a previous local suspension durable.
        for scenario_id in self._suspended:
            if (scenario := snapshot["scenarios"].get(scenario_id)) and scenario["status"] == "running":
                scenario.update(status="stopped", reason="storage_error")
        validate_running_intervals(snapshot["scenarios"])
        if (self.gateway is not None and self.gateway.handoff_supported
                and self._gateway_current(snapshot) is not None
                and not self.data["persons"].get(self.gateway.person_id, {}).get("enabled", True)):
            status = await self.gateway.status()
            latch = status.get("handoff")
            await self.gateway.handoff(self.gateway.key(snapshot["revision"], "own-baseline"), latch["id"] if latch else None)
            await self._async_gateway_handoff_reconcile(await self.gateway.status())
            snapshot["revision"] = self.data["revision"] + 1
            baseline = self.data["persons"][self.gateway.person_id]["current"]
            history = snapshot["persons"][self.gateway.person_id]["history"]
            if not any(timestamp(point["timestamp"]) == timestamp(baseline["timestamp"]) for point in history):
                if len(history) >= MAX_HISTORY:
                    raise LocationError("history_too_large")
                history.append(deepcopy(baseline))
                history.sort(key=lambda point: timestamp(point["timestamp"]))
        gateway_changed = self.gateway is not None and self._gateway_current(snapshot) != self._gateway_current(self.data)
        gateway_close = self.gateway is not None and (
            (self.data["enabled"] and not snapshot["enabled"])
            or (self.gateway.person_id in self.data["persons"] and self.gateway.person_id not in snapshot["persons"])
        )
        if self.gateway is not None and (gateway_changed or gateway_close or gateway_normal):
            # Invalidate the old normal epoch before touching persistent data.
            await self._async_gateway_close(snapshot["revision"])
        if (gateway_normal and self.data["persons"].get(self.gateway.person_id, {}).get("enabled")):
            # A previously committed explicit Own remains authoritative across
            # a failed index fence. Clear its old phone pointer before opening
            # a new period, while repeated Normal retains its accepted baseline.
            await self.location_history.async_own_current()
        await self.store.async_save(snapshot)
        if await self.store.async_load() != snapshot:
            raise LocationError("storage_error")
        self._require_loaded()
        self.data = snapshot
        if (self.gateway is not None and gateway_changed and self._gateway_current(snapshot) is not None):
            await self.location_history.async_own_current()
        self._retry_at = None
        self._storage_attempts = 0
        self._suspended.clear()
        if self.gateway is not None and (gateway_changed or self.gateway.pending):
            await self._async_gateway_sync()

    async def async_save(self, connection, token, revision, enabled, persons, *, quick_save=_UNSET, activity=_UNSET, gateway_mode=_UNSET):
        from .phone_activity import validate_activity

        async with self._save_lock:
            self.require_session(connection, token)
            return_status = await self._async_gateway_return_preflight()
            self.require_session(connection, token)
            if self.gateway is not None and self.gateway.handoff_supported and return_status is None:
                raise LocationError("gateway_return_unconfirmed")
            normal_generation = {"expected_id": return_status["scheduled_return"]["id"]
                if return_status["scheduled_return"] else None} if return_status is not None else {}
            if type(enabled) is not bool:
                raise LocationError("invalid_input")
            self._require_revision(revision)
            explicit_own = False
            if self.gateway is not None:
                raw = persons.get(self.gateway.person_id) if isinstance(persons, dict) else None
                previous = self.data["persons"].get(self.gateway.person_id)
                if isinstance(raw, dict) and previous is not None:
                    explicit_own = ("current_history_timestamp" in raw or raw.get("record_current_now") is True
                                    or coordinates(raw.get("current")) != coordinates(previous["current"]))
            snapshot = {
                "revision": revision + 1, "enabled": enabled,
                "persons": validate_persons(self.hass, persons, self.data["persons"], datetime.now(UTC)),
                "scenarios": deepcopy(self.data["scenarios"]),
                "quick_save": deepcopy(self.data["quick_save"]) if quick_save is _UNSET else validate_quick_save(self.hass, quick_save),
                "activity": deepcopy(self.data["activity"]) if activity is _UNSET else validate_activity(self.hass, activity, self.data["activity"]),
                **({"return_schedule": deepcopy(self.data["return_schedule"])} if "return_schedule" in self.data else {}),
            }
            if (self.gateway is not None and return_status is not None and return_status.get("handoff_supported")
                    and enabled and self.gateway.person_id in snapshot["persons"]
                    and snapshot["persons"][self.gateway.person_id]["enabled"]
                    and not self.data["persons"].get(self.gateway.person_id, {}).get("enabled", True)):
                await self._async_gateway_target_guard(snapshot)
                latch = return_status.get("handoff")
                await self.gateway.handoff(self.gateway.key(revision, "handoff"), latch["id"] if latch else None)
                await self._async_gateway_handoff_reconcile(await self.gateway.status())
                frozen = self.data["persons"][self.gateway.person_id]["current"]
                if not explicit_own:
                    snapshot["persons"][self.gateway.person_id]["current"] = deepcopy(frozen)
                history = snapshot["persons"][self.gateway.person_id]["history"]
                if not any(timestamp(item["timestamp"]) == timestamp(frozen["timestamp"]) for item in history):
                    history.append(deepcopy(frozen))
                    history.sort(key=lambda item: timestamp(item["timestamp"]))
                snapshot["revision"] = self.data["revision"] + 1
            if gateway_mode is not _UNSET:
                if (gateway_mode != "normal" or self.gateway is None or not enabled
                        or self._gateway_current(snapshot) is not None
                        or self.hass.states.get(self.gateway.person_id) is None):
                    raise LocationError("invalid_input")
            for scenario in snapshot["scenarios"].values():
                entity_id = scenario["entity_id"]
                changed_person = snapshot["persons"].get(entity_id) != self.data["persons"].get(entity_id)
                if (scenario["status"] == "completed"
                        and scenario.get("switch_state", {}).get("normal") in {"pending", "attempting", "started"}
                        and (not enabled or changed_person)):
                    scenario["switch_state"].update(normal="skipped", reason="normal_interrupted")
                if scenario["status"] == "running":
                    if not enabled:
                        scenario.update(status="stopped", reason="global_disabled")
                    elif changed_person and scenario["cursor"] > scenario.get("run_start_cursor", 0):
                        scenario.update(status="stopped", reason="manual_change")
            if (gateway_mode == "normal" and "return_schedule" in self.data
                    and self.source_schedule.conflict(snapshot, datetime.now(UTC))):
                raise LocationError("return_custom_conflict")
            await self._commit(snapshot, gateway_normal=gateway_mode == "normal")
            if explicit_own and self.gateway is not None and self._gateway_current(snapshot) is not None:
                await self.location_history.async_own_current()
            if gateway_mode == "normal" and self.gateway.ready and not self.gateway.pending:
                try:
                    if "return_schedule" in self.data:
                        status = await self.source_schedule.sync(opening=True)
                        normal_generation = {"expected_id": status["scheduled_return"]["id"]
                                             if status["scheduled_return"] else None}
                    key = self.gateway.key(snapshot["revision"], "normal")
                    intent_id = self.data["normal_intent"]["id"] if self.data.get("normal_intent") else None
                    await self.location_history.begin_normal(key, intent_id=intent_id)
                    await self.gateway.normal(key, intent_id=intent_id, **normal_generation)
                except GatewayError:
                    self._gateway_failed()  # Retry can resume only an already minted matching RAM entitlement.
                except asyncio.CancelledError:
                    self._gateway_failed()
                    raise
            self._reschedule()
            self.phone_activity.rebind()
            return self._snapshot()

    async def async_save_scenario(self, connection, token, revision, scenario_id, entity_id, day, name, points, *,
                                  routing=_UNSET, auto_custom=_UNSET, auto_ha=_UNSET, point_names_version=_UNSET):
        async with self._save_lock:
            self.require_session(connection, token)
            await self._async_gateway_return_preflight()
            self.require_session(connection, token)
            self._require_revision(revision)
            if (
                not isinstance(entity_id, str) or not entity_id.startswith("person.")
                or self.hass.states.get(entity_id) is None
                or not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                or (scenario_id is not None and not isinstance(scenario_id, str))
            ):
                raise LocationError("invalid_input")
            day = scenario_day(day)
            previous = self.data["scenarios"].get(scenario_id) if scenario_id is not None else None
            if scenario_id is not None and previous is None:
                raise LocationError("scenario_missing")
            if previous and previous["entity_id"] != entity_id:
                raise LocationError("invalid_input")
            if point_names_version is not _UNSET and (type(point_names_version) is not int or point_names_version != 1):
                raise LocationError("invalid_input")
            if previous and any("name" in point for point in previous["points"]) and point_names_version is _UNSET:
                raise LocationError("client_outdated")
            if ((auto_custom is not _UNSET and type(auto_custom) is not bool)
                    or (auto_ha is not _UNSET and type(auto_ha) is not bool)):
                raise LocationError("invalid_input")
            modern = auto_custom is not _UNSET or auto_ha is not _UNSET or bool(previous and "auto_custom" in previous)
            custom_option = previous.get("auto_custom", True) if auto_custom is _UNSET and previous else (True if auto_custom is _UNSET else auto_custom)
            ha_option = previous.get("auto_ha", False) if auto_ha is _UNSET and previous else (False if auto_ha is _UNSET else auto_ha)
            if ha_option and (self.gateway is None or entity_id != self.gateway.person_id or not self.gateway.return_supported):
                raise LocationError("gateway_return_unavailable")
            if routing is _UNSET and previous and (
                "routing" in previous or any("stop" in point for point in previous["points"])
            ):
                raise LocationError("client_outdated")
            normalized_routing = None if routing is _UNSET else scenario_routing(routing)
            if previous is None and len(self.data["scenarios"]) >= MAX_SCENARIOS:
                raise LocationError("scenario_limit")
            cursor = previous["cursor"] if previous else 0
            if previous and cursor > 0 and day != previous["day"]:
                raise LocationError("executed_prefix")
            normalized = scenario_points(self.hass, points, day=day, allow_empty=True)
            if previous and normalized[:cursor] != previous["points"][:cursor]:
                raise LocationError("executed_prefix")
            status = previous["status"] if previous else "draft"
            reason = previous["reason"] if previous else ""
            if scenario_id in self._suspended:
                status, reason = "stopped", "storage_error"
            if status == "completed" and len(normalized) > cursor:
                status, reason = "stopped", ""
            if scenario_id is None:
                scenario_id = secrets.token_hex(16)
            snapshot = deepcopy(self.data)
            snapshot["revision"] += 1
            snapshot["scenarios"][scenario_id] = {
                "entity_id": entity_id, "day": day, "name": name.strip(), "points": normalized,
                "status": status, "cursor": cursor, "reason": reason,
            }
            saved = snapshot["scenarios"][scenario_id]
            if modern or previous and "run_start_cursor" in previous:
                saved.update(auto_custom=custom_option, auto_ha=ha_option,
                             run_start_cursor=previous.get("run_start_cursor", 0) if previous else 0,
                             run_id=previous.get("run_id") if previous else None,
                             switch_cursor=previous.get("switch_cursor", cursor) if previous else cursor,
                             switch_state=deepcopy(previous.get("switch_state", switch_state(False, False))) if previous else switch_state(False, False))
                changed_schedule = previous and (
                    [{k: p[k] for k in ("latitude", "longitude", "timestamp")} for p in normalized] !=
                    [{k: p[k] for k in ("latitude", "longitude", "timestamp")} for p in previous["points"]]
                    or custom_option != previous.get("auto_custom", True) or ha_option != previous.get("auto_ha", False))
                if status == "running" and changed_schedule:
                    saved.update(run_id=secrets.token_hex(16), switch_cursor=cursor,
                                 switch_state=switch_state(custom_option, ha_option))
            if normalized_routing is not None:
                snapshot["scenarios"][scenario_id]["routing"] = normalized_routing
            if status == "running":
                if cursor == len(normalized):
                    raise LocationError("no_pending_points")
                now = datetime.now(UTC)
                if any(timestamp(point["timestamp"]) <= now for point in normalized[cursor:]):
                    raise LocationError("pending_in_past")
                if problem := self._scenario_problem(snapshot, entity_id, snapshot["scenarios"][scenario_id], waiting=True):
                    raise LocationError(problem)
            if await self._async_cancel_route_warmup(previous, saved):
                snapshot["persons"][self.gateway.person_id] = deepcopy(self.data["persons"][self.gateway.person_id])
                snapshot["revision"] = self.data["revision"] + 1
            await self._commit(snapshot)
            if self.gateway is not None and "return_schedule" in self.data:
                await self.source_schedule.tick()
            self._reschedule()
            return self._snapshot()

    def _scenario_problem(self, snapshot, entity_id, scenario, *, waiting=False):
        if not snapshot["enabled"]:
            return "global_disabled"
        if self.hass.states.get(entity_id) is None:
            return "person_missing"
        person = snapshot["persons"].get(entity_id)
        if person is None and len(snapshot["persons"]) >= MAX_PERSONS:
            return "person_limit"
        protected = self.gateway is not None and entity_id == self.gateway.person_id
        if protected and (not person or not person["enabled"]):
            if not waiting:
                return "person_disabled"
        elif person and not person["enabled"]:
            return "person_disabled"
        history = person["history"] if person else []
        pending = scenario["points"][scenario["cursor"]:]
        if len(history) + len(pending) > MAX_HISTORY:
            return "history_limit"
        seen = {point["timestamp"] for point in history}
        if any(point["timestamp"] in seen for point in pending):
            return "history_conflict"
        return ""

    def _advance_scenarios(self, snapshot, now):
        """Catch up in one snapshot, preserving each original point timestamp."""
        changed = False
        running = sorted(
            ((scenario_id, scenario) for scenario_id, scenario in snapshot["scenarios"].items()
             if scenario["status"] == "running" and scenario_id not in self._suspended),
            key=lambda item: item[1]["points"][item[1]["cursor"]]["timestamp"],
        )
        for scenario_id, scenario in running:
            entity_id = scenario["entity_id"]
            pending_at = timestamp(scenario["points"][scenario["cursor"]]["timestamp"])
            if pending_at > now:
                # A waiting route may intentionally remain in HA until its own deadline.
                if not snapshot["enabled"] or self.hass.states.get(entity_id) is None:
                    scenario.update(status="stopped", reason="global_disabled" if not snapshot["enabled"] else "person_missing")
                    changed = True
                continue
            if (self.gateway is not None and entity_id == self.gateway.person_id
                    and snapshot["persons"].get(entity_id, {}).get("enabled") is True
                    and not self.source_schedule.custom_ready):
                if not scenario.get("auto_custom", True):
                    scenario.update(status="stopped", reason="person_disabled")
                    changed = True
                else:
                    self._retry_at = now + timedelta(seconds=5)
                continue
            if reason := self._scenario_problem(snapshot, entity_id, scenario):
                scenario.update(status="stopped", reason=reason)
                changed = True
                continue
            due = []
            for point in scenario["points"][scenario["cursor"]:]:
                if timestamp(point["timestamp"]) > now:
                    break
                due.append({key: point[key] for key in ("latitude", "longitude", "timestamp")})
            if not due:
                continue
            person = snapshot["persons"].setdefault(entity_id, {"enabled": True, "history": []})
            person["history"].extend(due)
            person["history"].sort(key=lambda point: point["timestamp"])
            if "current" not in person or person["current"]["timestamp"] < due[-1]["timestamp"]:
                person["current"] = deepcopy(due[-1])
            scenario["cursor"] += len(due)
            if scenario["cursor"] == len(scenario["points"]):
                scenario.update(status="completed", reason="")
            changed = True
        return changed

    async def async_scenario_action(self, connection, token, revision, scenario_id, action):
        async with self._save_lock:
            self.require_session(connection, token)
            await self._async_gateway_return_preflight()
            self.require_session(connection, token)
            self._require_revision(revision)
            if not isinstance(scenario_id, str) or not isinstance(action, str) or action not in {"start", "stop", "delete"}:
                raise LocationError("invalid_input")
            existing = self.data["scenarios"].get(scenario_id)
            if (existing is not None and self.gateway is not None and existing["entity_id"] == self.gateway.person_id
                    and self.gateway.return_supported and action == "start"):
                try:
                    await self.source_schedule.initialize(await self.gateway.status())
                except GatewayError:
                    self._gateway_failed()
                    raise LocationError("return_status_unavailable") from None
            snapshot = self._snapshot()
            scenario = snapshot["scenarios"].get(scenario_id)
            if scenario is None:
                raise LocationError("scenario_missing")
            entity_id = scenario["entity_id"]
            if action == "delete":
                if await self._async_cancel_route_warmup(scenario, None):
                    snapshot["persons"][self.gateway.person_id] = deepcopy(self.data["persons"][self.gateway.person_id])
                    snapshot["revision"] = self.data["revision"]
                del snapshot["scenarios"][scenario_id]
            elif action == "stop":
                if await self._async_cancel_route_warmup(scenario, None):
                    snapshot["persons"][self.gateway.person_id] = deepcopy(self.data["persons"][self.gateway.person_id])
                    snapshot["revision"] = self.data["revision"]
                scenario.update(status="stopped", reason="user_stop")
            else:
                if self.gateway is not None and entity_id == self.gateway.person_id:
                    await self._async_gateway_target_guard(start=True)
                if scenario["status"] == "running" and scenario_id not in self._suspended:
                    raise LocationError("scenario_running")
                if scenario["cursor"] == len(scenario["points"]):
                    raise LocationError("no_pending_points")
                if (self.gateway is not None and entity_id == self.gateway.person_id
                        and not scenario.get("auto_custom", True)):
                    try:
                        confirmed = self.source_schedule.confirmed_custom(await self.gateway.status())
                    except GatewayError:
                        self._gateway_failed()
                        raise LocationError("return_status_unavailable") from None
                    if not snapshot["persons"].get(entity_id, {}).get("enabled", False) or not confirmed:
                        raise LocationError("person_disabled")
                if reason := self._scenario_problem(snapshot, entity_id, scenario, waiting=True):
                    raise LocationError(reason)
                scenario.update(status="running", reason="", run_start_cursor=scenario["cursor"],
                                run_id=secrets.token_hex(16), switch_cursor=scenario["cursor"],
                                auto_custom=scenario.get("auto_custom", True), auto_ha=scenario.get("auto_ha", False),
                                switch_state=switch_state(scenario.get("auto_custom", True), scenario.get("auto_ha", False)))
                if (self.gateway is not None and entity_id == self.gateway.person_id
                        and self.gateway.return_supported):
                    snapshot.setdefault("return_schedule", empty_schedule())
                validate_running_intervals(snapshot["scenarios"])
                # Explicit Start is permission to recover this suspended plan.
                suspended = scenario_id in self._suspended
                self._suspended.discard(scenario_id)
                try:
                    if self.gateway is None:
                        self._advance_scenarios(snapshot, datetime.now(UTC))
                    snapshot["revision"] += 1
                    await self._commit(snapshot)
                    if self.gateway is not None:
                        await self.source_schedule.tick()
                        snapshot = deepcopy(self.data)
                        if self._advance_scenarios(snapshot, datetime.now(UTC)):
                            snapshot["revision"] += 1
                            await self._commit(snapshot)
                        await self.source_schedule.tick()
                except Exception:
                    if suspended:
                        self._suspended.add(scenario_id)
                    raise
                self._reschedule()
                return self._snapshot()
            snapshot["revision"] += 1
            await self._commit(snapshot)
            if self.gateway is not None and "return_schedule" in self.data:
                await self.source_schedule.tick()
            self._reschedule()
            return self._snapshot()

    async def async_start(self):
        """Begin restored plans only after integration and HA startup are complete."""
        self._started = True
        _REFRESH_LOGGER.info("Location refresh diagnostics ready")
        self.phone_activity.start()
        if self.hass.state == CoreState.running:
            await self._async_begin()
        else:
            self._startup_cancel = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._async_ha_started,
            )

    async def _async_ha_started(self, event):
        self._startup_cancel = None
        await self._async_begin()

    async def _async_begin(self):
        if self.gateway is not None:
            async with self._save_lock:
                if not self._normal_intent_valid():
                    await self._async_gateway_close(self.data["revision"])
            # Reconcile history and the live entitlement before admitting a new session.
            async with self._save_lock:
                await self._async_gateway_sync()
                try:
                    await self.location_history.async_prepare()
                    await self.location_history.async_recover()
                except Exception:
                    if self.location_history.db is not None and self.location_history.db.pending:
                        self._gateway_failed()
        await self._async_run_due(datetime.now(UTC))
        if self.gateway is not None:
            async with self._save_lock:
                await self._async_gateway_sync()
                if self.gateway.return_supported or self.gateway.resume_supported:
                    self._schedule_gateway_retry()

    def _reschedule(self):
        for name in ("_timer_cancel", "_state_cancel"):
            if cancel := getattr(self, name):
                cancel()
                setattr(self, name, None)
        if not self.loaded or not self._started:
            return
        running = {
            scenario_id: scenario for scenario_id, scenario in self.data["scenarios"].items()
            if scenario["status"] == "running" and scenario_id not in self._suspended
        }
        deadlines = self.source_schedule.deadlines()
        if not running and not deadlines:
            return
        if running:
            self._state_cancel = async_track_state_change_event(
                self.hass, sorted({scenario["entity_id"] for scenario in running.values()}), self._async_person_changed,
            )
        deadlines.extend(timestamp(scenario["points"][scenario["cursor"]]["timestamp"]) for scenario in running.values())
        deadline = self._retry_at or min(deadlines)
        self._timer_cancel = async_track_point_in_utc_time(self.hass, self._async_run_due, deadline)

    async def _async_person_changed(self, event):
        # Changes in real GPS coordinates never alter a plan. Removal is a stop.
        if event.data.get("new_state") is None:
            await self._async_run_due(datetime.now(UTC))

    async def _async_run_due(self, now):
        async with self._save_lock:
            if not self.loaded:
                return
            if self._location_history is not None and self._location_history.correction_active:
                self._schedule_gateway_retry()
                return
            try:
                await self._async_gateway_return_preflight()
                if self.gateway is not None:
                    await self.source_schedule.tick()
            except Exception:
                if self.gateway is not None:
                    self._gateway_failed()
                self._retry_at = datetime.now(UTC) + timedelta(seconds=5)
                self._reschedule()
                self._schedule_gateway_retry()
                return
            if (self._location_history is not None and self._location_history.db is not None
                    and self._location_history.db.pending):
                self._retry_at = datetime.now(UTC) + timedelta(seconds=5)
                self._reschedule()
                return
            self._retry_at = None
            # HA passes the originally requested deadline to this callback, even
            # when it fires late. Catch up to actual UTC now in one transaction.
            now = datetime.now(UTC)
            snapshot = deepcopy(self.data)
            if self._advance_scenarios(snapshot, now):
                snapshot["revision"] += 1
                try:
                    await self._commit(snapshot)
                    if self.gateway is not None:
                        await self.source_schedule.tick()
                except Exception:
                    if not self.loaded:
                        return
                    self._storage_attempts += 1
                    _LOGGER.error("Manual Location scenario storage unavailable; attempt %s", self._storage_attempts)
                    if self._storage_attempts < SCENARIO_STORAGE_ATTEMPTS:
                        self._retry_at = datetime.now(UTC) + timedelta(seconds=SCENARIO_RETRY_SECONDS)
                    else:
                        self._suspended.update(
                            key for key, value in self.data["scenarios"].items() if value["status"] == "running"
                        )
                        stopped = deepcopy(self.data)
                        stopped["revision"] += 1
                        try:
                            await self._commit(stopped)
                        except Exception:
                            _LOGGER.error("Manual Location scenario execution suspended until storage recovery")
            self._reschedule()

    async def async_shutdown(self):
        """Cancel timers/listeners and drain the one writer before unloading."""
        self.loaded = False
        if self.gateway is not None:
            self.gateway.ready = False
        if self._location_history is not None:
            self._location_history.stop()
        if self._gateway_retry_cancel is not None:
            self._gateway_retry_cancel()
            self._gateway_retry_cancel = None
        for task, cleanup in list(self._refresh_cleanups.items()):
            cleanup()
            task.cancel()
        self._refresh_cleanups.clear()
        if self._phone_activity is not None:
            await self.phone_activity.async_shutdown()
        for name in ("_timer_cancel", "_state_cancel", "_startup_cancel"):
            if cancel := getattr(self, name):
                cancel()
                setattr(self, name, None)
        async with self._save_lock:
            if self.gateway is not None:
                self.gateway.ready = False
            self.sessions.clear()
        if self._location_history is not None:
            await self._location_history.async_shutdown()

    async def async_change_pin(self, connection, token, new_pin):
        async with self._auth_lock:
            self.require_session(connection, token)
            if not valid_pin(new_pin):
                raise LocationError("invalid_input")
            pin_hash = await self.hass.async_add_executor_job(hash_pin, new_pin)
            self.require_session(connection, token)
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, "pin_hash": pin_hash})
            self.sessions.clear()
            return None

    def lock(self, connection, token):
        self.require_session(connection, token)
        self.sessions.pop(token)

    def view(self, connection, entity_ids, start, end):
        self._require_loaded()
        if not isinstance(entity_ids, list) or len(entity_ids) > MAX_PERSONS:
            raise LocationError("invalid_input")
        for entity_id in entity_ids:
            if not isinstance(entity_id, str) or not entity_id.startswith("person."):
                raise LocationError("invalid_input")
            if connection.user is None or not connection.user.permissions.check_entity(entity_id, POLICY_READ):
                raise LocationError("unauthorized")
        start_at, end_at = timestamp(start), timestamp(end)
        if end_at < start_at or end_at - start_at > timedelta(days=31):
            raise LocationError("invalid_input")
        overrides = {}
        if self.data["enabled"]:
            for entity_id in entity_ids:
                person = self.data["persons"].get(entity_id)
                if not person or not person["enabled"]:
                    continue
                history, preceding = [], None
                for point in person["history"]:
                    instant = timestamp(point["timestamp"])
                    if instant < start_at:
                        preceding = point
                    elif instant <= end_at:
                        history.append(point)
                if preceding is not None:
                    history.insert(0, preceding)
                overrides[entity_id] = {"current": person["current"], "history": history}
        activity = {entity_id: self.phone_activity.summary(connection.user, entity_id) for entity_id in entity_ids}
        return deepcopy({"revision": self.data["revision"], "overrides": overrides, "activity": activity})

    async def async_view(self, connection, entity_ids, start, end):
        for attempt in range(2):
            result = self.view(connection, entity_ids, start, end)
            if self.gateway is not None:
                try:
                    result = await self.location_history.async_view(result, entity_ids, start, end)
                except LocationError as error:
                    if error.code == "history_changed" and attempt == 0:
                        continue
                    raise
                # Recheck entity permissions after the asynchronous history read.
                self.view(connection, entity_ids, start, end)
            return result
