"""Persistent manual positions and connection-bound administrative sessions."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import math
import logging
import secrets
import time
import unicodedata

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, MAX_HISTORY, MAX_PERSONS, MAX_SCENARIOS, MAX_SCENARIO_POINTS, PIN_MAX_ATTEMPTS, PIN_WINDOW_SECONDS,
    SCENARIO_RETRY_SECONDS, SCENARIO_STORAGE_ATTEMPTS, SESSION_SECONDS, STORAGE_VERSION,
)
from .security import hash_pin, valid_pin, verify_pin

_LOGGER = logging.getLogger(__name__)
_UNSET = object()


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
        if isinstance(point, dict) and "stop" in point:
            normalized = coordinates({key: value for key, value in point.items() if key != "stop"})
            normalized["stop"] = stop_label(point["stop"])
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
            or set(scenario) not in (new_fields, new_fields | {"routing"})
            or not isinstance(scenario["entity_id"], str) or not scenario["entity_id"].startswith("person.")
            or not isinstance(scenario["name"], str) or not 1 <= len(scenario["name"].strip()) <= 80
            or scenario["status"] not in {"draft", "running", "stopped", "completed"}
            or type(scenario["cursor"]) is not int
            or not isinstance(scenario["reason"], str) or len(scenario["reason"]) > 80
        ):
            raise LocationError("invalid_input")
        scenario_day(scenario["day"])
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
            )
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
        if "current_history_timestamp" in settings:
            current_at = timestamp(settings["current_history_timestamp"]).isoformat()
            selected = next((point for point in history if point["timestamp"] == current_at), None)
            if selected is None or any(current[k] != selected[k] for k in current):
                raise LocationError("invalid_input")
            # Consume the draft marker; only the validated historical point persists.
            current = deepcopy(selected)
        elif old_current is not None and all(current[k] == old_current[k] for k in current):
            current = deepcopy(old_current)
        else:
            if now in seen or len(history) >= MAX_HISTORY:
                raise LocationError("invalid_input")
            current["timestamp"] = now.isoformat()
            history.append(deepcopy(current))
        history.sort(key=lambda point: point["timestamp"])
        result[entity_id] = {"enabled": settings["enabled"], "current": current, "history": history}
    return result


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
        self.data = {"revision": 0, "enabled": True, "persons": {}, "scenarios": {}}
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

    @property
    def geocoder(self):
        """Initialize optional search only when an administrator requests it."""
        if self._geocoder is None:
            from .geocoding import PlaceGeocoder

            self._geocoder = PlaceGeocoder(self.hass, self.entry)
        return self._geocoder

    async def async_load(self):
        stored = await self.store.async_load()
        if stored is None:
            if self.entry.data.get("storage_initialized"):
                raise LocationError("storage_error")
            await self.store.async_save(self.data)
            if await self.store.async_load() != self.data:
                raise LocationError("storage_error")
        else:
            if (
                not isinstance(stored, dict) or set(stored) not in (
                    {"revision", "enabled", "persons"}, {"revision", "enabled", "persons", "scenarios"},
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

    def _snapshot(self):
        snapshot = deepcopy(self.data)
        # A failed disk cannot persist a stop. Report the local safety suspension
        # without pretending the durable cursor or snapshot has advanced.
        for scenario_id in self._suspended:
            if scenario := snapshot["scenarios"].get(scenario_id):
                scenario.update(status="stopped", reason="storage_error")
        return snapshot

    def _require_revision(self, revision):
        if type(revision) is not int:
            raise LocationError("invalid_input")
        if revision != self.data["revision"]:
            raise LocationError("revision_conflict")

    async def _commit(self, snapshot):
        """Commit history/current/cursors together and confirm actual persistence."""
        self._require_loaded()
        # Any later successful write makes a previous local suspension durable.
        for scenario_id in self._suspended:
            if (scenario := snapshot["scenarios"].get(scenario_id)) and scenario["status"] == "running":
                scenario.update(status="stopped", reason="storage_error")
        validate_running_intervals(snapshot["scenarios"])
        await self.store.async_save(snapshot)
        if await self.store.async_load() != snapshot:
            raise LocationError("storage_error")
        self._require_loaded()
        self.data = snapshot
        self._retry_at = None
        self._storage_attempts = 0
        self._suspended.clear()

    async def async_save(self, connection, token, revision, enabled, persons):
        async with self._save_lock:
            self.require_session(connection, token)
            if type(enabled) is not bool:
                raise LocationError("invalid_input")
            self._require_revision(revision)
            snapshot = {
                "revision": revision + 1, "enabled": enabled,
                "persons": validate_persons(self.hass, persons, self.data["persons"], datetime.now(UTC)),
                "scenarios": deepcopy(self.data["scenarios"]),
            }
            for scenario in snapshot["scenarios"].values():
                entity_id = scenario["entity_id"]
                if scenario["status"] == "running":
                    if not enabled:
                        scenario.update(status="stopped", reason="global_disabled")
                    elif snapshot["persons"].get(entity_id) != self.data["persons"].get(entity_id):
                        scenario.update(status="stopped", reason="manual_change")
            await self._commit(snapshot)
            self._reschedule()
            return self._snapshot()

    async def async_save_scenario(self, connection, token, revision, scenario_id, entity_id, day, name, points, *, routing=_UNSET):
        async with self._save_lock:
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
            if normalized_routing is not None:
                snapshot["scenarios"][scenario_id]["routing"] = normalized_routing
            if status == "running":
                if cursor == len(normalized):
                    raise LocationError("no_pending_points")
                now = datetime.now(UTC)
                if any(timestamp(point["timestamp"]) <= now for point in normalized[cursor:]):
                    raise LocationError("pending_in_past")
                if problem := self._scenario_problem(snapshot, entity_id, snapshot["scenarios"][scenario_id]):
                    raise LocationError(problem)
            await self._commit(snapshot)
            self._reschedule()
            return self._snapshot()

    def _scenario_problem(self, snapshot, entity_id, scenario):
        if not snapshot["enabled"]:
            return "global_disabled"
        if self.hass.states.get(entity_id) is None:
            return "person_missing"
        person = snapshot["persons"].get(entity_id)
        if person is None and len(snapshot["persons"]) >= MAX_PERSONS:
            return "person_limit"
        if person and not person["enabled"]:
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
            self._require_revision(revision)
            if not isinstance(scenario_id, str) or not isinstance(action, str) or action not in {"start", "stop", "delete"}:
                raise LocationError("invalid_input")
            snapshot = self._snapshot()
            scenario = snapshot["scenarios"].get(scenario_id)
            if scenario is None:
                raise LocationError("scenario_missing")
            entity_id = scenario["entity_id"]
            if action == "delete":
                del snapshot["scenarios"][scenario_id]
            elif action == "stop":
                scenario.update(status="stopped", reason="user_stop")
            else:
                if scenario["status"] == "running" and scenario_id not in self._suspended:
                    raise LocationError("scenario_running")
                if scenario["cursor"] == len(scenario["points"]):
                    raise LocationError("no_pending_points")
                if reason := self._scenario_problem(snapshot, entity_id, scenario):
                    raise LocationError(reason)
                scenario.update(status="running", reason="")
                validate_running_intervals(snapshot["scenarios"])
                # Explicit Start is permission to recover this suspended plan.
                suspended = scenario_id in self._suspended
                self._suspended.discard(scenario_id)
                try:
                    self._advance_scenarios(snapshot, datetime.now(UTC))
                    snapshot["revision"] += 1
                    await self._commit(snapshot)
                except Exception:
                    if suspended:
                        self._suspended.add(scenario_id)
                    raise
                self._reschedule()
                return self._snapshot()
            snapshot["revision"] += 1
            await self._commit(snapshot)
            self._reschedule()
            return self._snapshot()

    async def async_start(self):
        """Begin restored plans only after integration and HA startup are complete."""
        self._started = True
        if self.hass.state == CoreState.running:
            await self._async_run_due(datetime.now(UTC))
        else:
            self._startup_cancel = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._async_ha_started,
            )

    async def _async_ha_started(self, event):
        self._startup_cancel = None
        await self._async_run_due(datetime.now(UTC))

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
        if not running:
            return
        self._state_cancel = async_track_state_change_event(
            self.hass, sorted({scenario["entity_id"] for scenario in running.values()}), self._async_person_changed,
        )
        deadline = self._retry_at or min(
            timestamp(scenario["points"][scenario["cursor"]]["timestamp"])
            for scenario in running.values()
        )
        self._timer_cancel = async_track_point_in_utc_time(self.hass, self._async_run_due, deadline)

    async def _async_person_changed(self, event):
        # Changes in real GPS coordinates never alter a plan. Removal is a stop.
        if event.data.get("new_state") is None:
            await self._async_run_due(datetime.now(UTC))

    async def _async_run_due(self, now):
        async with self._save_lock:
            if not self.loaded:
                return
            # HA passes the originally requested deadline to this callback, even
            # when it fires late. Catch up to actual UTC now in one transaction.
            now = datetime.now(UTC)
            snapshot = deepcopy(self.data)
            if self._advance_scenarios(snapshot, now):
                snapshot["revision"] += 1
                try:
                    await self._commit(snapshot)
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
        for name in ("_timer_cancel", "_state_cancel", "_startup_cancel"):
            if cancel := getattr(self, name):
                cancel()
                setattr(self, name, None)
        async with self._save_lock:
            self.sessions.clear()

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
        return deepcopy({"revision": self.data["revision"], "overrides": overrides})
