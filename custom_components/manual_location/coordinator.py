"""Persistent manual positions and connection-bound administrative sessions."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
import logging
import secrets
import time

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, MAX_HISTORY, MAX_PERSONS, MAX_SCENARIO_POINTS, PIN_MAX_ATTEMPTS, PIN_WINDOW_SECONDS,
    SCENARIO_RETRY_SECONDS, SCENARIO_STORAGE_ATTEMPTS, SESSION_SECONDS, STORAGE_VERSION,
)
from .security import hash_pin, valid_pin, verify_pin

_LOGGER = logging.getLogger(__name__)


def scenario_points(hass, points: object, *, check_day=True) -> list[dict]:
    """Normalize a one-day plan in the configured Home Assistant timezone."""
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_SCENARIO_POINTS:
        raise LocationError("invalid_input")
    zone = dt_util.get_time_zone(hass.config.time_zone) if check_day else None
    if check_day and zone is None:
        raise LocationError("invalid_input")
    result, instants, days = [], set(), set()
    for point in points:
        normalized = coordinates(point)
        instant = timestamp(point.get("timestamp"))
        if instant in instants:
            raise LocationError("invalid_input")
        instants.add(instant)
        if check_day:
            try:
                days.add(instant.astimezone(zone).date())
            except (ValueError, OverflowError):
                raise LocationError("invalid_input") from None
        result.append(normalized | {"timestamp": instant.isoformat()})
    if check_day and len(days) != 1:
        raise LocationError("invalid_input")
    return sorted(result, key=lambda point: point["timestamp"])


def validate_scenarios(hass, scenarios: object) -> dict:
    """Validate persisted plans, without requiring their people to exist at startup."""
    if not isinstance(scenarios, dict) or len(scenarios) > MAX_PERSONS:
        raise LocationError("invalid_input")
    result = {}
    for entity_id, scenario in scenarios.items():
        if (
            not isinstance(entity_id, str) or not entity_id.startswith("person.")
            or not isinstance(scenario, dict)
            or set(scenario) != {"name", "points", "status", "cursor", "reason"}
            or not isinstance(scenario["name"], str) or not 1 <= len(scenario["name"].strip()) <= 80
            or scenario["status"] not in {"draft", "running", "stopped", "completed"}
            or type(scenario["cursor"]) is not int
            or not isinstance(scenario["reason"], str) or len(scenario["reason"]) > 80
        ):
            raise LocationError("invalid_input")
        # A timezone change must not invalidate a plan accepted in the old zone.
        # Persisted points retain their UTC ordering and immutable executed prefix.
        points = scenario_points(hass, scenario["points"], check_day=False)
        cursor = scenario["cursor"]
        if (
            not 0 <= cursor <= len(points)
            or (scenario["status"] == "draft" and cursor != 0)
            or (scenario["status"] == "running" and cursor == len(points))
            or (scenario["status"] == "completed" and cursor != len(points))
            or points != scenario["points"]
        ):
            raise LocationError("invalid_input")
        result[entity_id] = deepcopy(scenario)
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
            now = datetime.now(UTC)
            try:
                for person in stored["persons"].values():
                    if timestamp(person["current"]["timestamp"]) > now:
                        raise LocationError("storage_error")
                stored["persons"] = validate_persons(
                    self.hass, stored["persons"], stored["persons"], now, check_entities=False
                )
                stored["scenarios"] = validate_scenarios(self.hass, stored.get("scenarios", {}))
            except (LocationError, KeyError, TypeError):
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
        for entity_id in self._suspended:
            if scenario := snapshot["scenarios"].get(entity_id):
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
        for entity_id in self._suspended:
            if (scenario := snapshot["scenarios"].get(entity_id)) and scenario["status"] == "running":
                scenario.update(status="stopped", reason="storage_error")
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
            for entity_id, scenario in snapshot["scenarios"].items():
                if scenario["status"] == "running":
                    if not enabled:
                        scenario.update(status="stopped", reason="global_disabled")
                    elif snapshot["persons"].get(entity_id) != self.data["persons"].get(entity_id):
                        scenario.update(status="stopped", reason="manual_change")
            await self._commit(snapshot)
            self._reschedule()
            return self._snapshot()

    async def async_save_scenario(self, connection, token, revision, entity_id, name, points):
        async with self._save_lock:
            self.require_session(connection, token)
            self._require_revision(revision)
            if (
                not isinstance(entity_id, str) or not entity_id.startswith("person.")
                or self.hass.states.get(entity_id) is None
                or not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
            ):
                raise LocationError("invalid_input")
            previous = self.data["scenarios"].get(entity_id)
            if previous and previous["status"] == "running" and entity_id not in self._suspended:
                raise LocationError("scenario_running")
            if previous is None and len(self.data["scenarios"]) >= MAX_PERSONS:
                raise LocationError("invalid_input")
            normalized = scenario_points(self.hass, points)
            cursor = previous["cursor"] if previous else 0
            if previous and normalized[:cursor] != previous["points"][:cursor]:
                raise LocationError("executed_prefix")
            status = previous["status"] if previous else "draft"
            reason = previous["reason"] if previous else ""
            if entity_id in self._suspended:
                status, reason = "stopped", "storage_error"
            if status == "completed" and len(normalized) > cursor:
                status, reason = "stopped", ""
            snapshot = deepcopy(self.data)
            snapshot["revision"] += 1
            snapshot["scenarios"][entity_id] = {
                "name": name.strip(), "points": normalized, "status": status,
                "cursor": cursor, "reason": reason,
            }
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
        for entity_id, scenario in snapshot["scenarios"].items():
            if scenario["status"] != "running" or entity_id in self._suspended:
                continue
            if reason := self._scenario_problem(snapshot, entity_id, scenario):
                scenario.update(status="stopped", reason=reason)
                changed = True
                continue
            due = []
            for point in scenario["points"][scenario["cursor"]:]:
                if timestamp(point["timestamp"]) > now:
                    break
                due.append(deepcopy(point))
            if not due:
                continue
            person = snapshot["persons"].setdefault(entity_id, {"enabled": True, "history": []})
            person["history"].extend(due)
            person["history"].sort(key=lambda point: point["timestamp"])
            person["current"] = deepcopy(due[-1])
            scenario["cursor"] += len(due)
            if scenario["cursor"] == len(scenario["points"]):
                scenario.update(status="completed", reason="")
            changed = True
        return changed

    async def async_scenario_action(self, connection, token, revision, entity_id, action):
        async with self._save_lock:
            self.require_session(connection, token)
            self._require_revision(revision)
            if not isinstance(entity_id, str) or not isinstance(action, str) or action not in {"start", "stop", "delete"}:
                raise LocationError("invalid_input")
            snapshot = deepcopy(self.data)
            scenario = snapshot["scenarios"].get(entity_id)
            if scenario is None:
                raise LocationError("scenario_missing")
            if action == "delete":
                del snapshot["scenarios"][entity_id]
            elif action == "stop":
                scenario.update(status="stopped", reason="user_stop")
            else:
                if scenario["status"] == "running" and entity_id not in self._suspended:
                    raise LocationError("scenario_running")
                if scenario["cursor"] == len(scenario["points"]):
                    raise LocationError("no_pending_points")
                if reason := self._scenario_problem(snapshot, entity_id, scenario):
                    raise LocationError(reason)
                scenario.update(status="running", reason="")
                # Explicit Start is permission to recover this suspended plan.
                suspended = entity_id in self._suspended
                self._suspended.discard(entity_id)
                try:
                    self._advance_scenarios(snapshot, datetime.now(UTC))
                    snapshot["revision"] += 1
                    await self._commit(snapshot)
                except Exception:
                    if suspended:
                        self._suspended.add(entity_id)
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
            entity_id: scenario for entity_id, scenario in self.data["scenarios"].items()
            if scenario["status"] == "running" and entity_id not in self._suspended
        }
        if not running:
            return
        self._state_cancel = async_track_state_change_event(self.hass, list(running), self._async_person_changed)
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
