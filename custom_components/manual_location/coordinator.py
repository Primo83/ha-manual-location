"""Persistent manual positions and connection-bound administrative sessions."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
import secrets
import time

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN, MAX_HISTORY, MAX_PERSONS, PIN_MAX_ATTEMPTS, PIN_WINDOW_SECONDS,
    SESSION_SECONDS, STORAGE_VERSION,
)
from .security import hash_pin, valid_pin, verify_pin


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
            or set(settings) != {"enabled", "current", "history"}
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
        if old_current is not None and all(current[k] == old_current[k] for k in current):
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
        self.data = {"revision": 0, "enabled": True, "persons": {}}
        self.sessions: dict[str, Session] = {}
        self.failures: dict[str, list[float]] = {}
        self.blocked_until: dict[str, float] = {}
        self._auth_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self.loaded = True

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
                not isinstance(stored, dict) or set(stored) != {"revision", "enabled", "persons"}
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
        return deepcopy(self.data)

    async def async_save(self, connection, token, revision, enabled, persons):
        async with self._save_lock:
            self.require_session(connection, token)
            if type(revision) is not int or type(enabled) is not bool:
                raise LocationError("invalid_input")
            if revision != self.data["revision"]:
                raise LocationError("revision_conflict")
            snapshot = {
                "revision": revision + 1, "enabled": enabled,
                "persons": validate_persons(self.hass, persons, self.data["persons"], datetime.now(UTC)),
            }
            await self.store.async_save(snapshot)
            # Store logs and swallows disk WriteError. Read back via its public API
            # before confirming a save or advancing the revision in memory.
            if await self.store.async_load() != snapshot:
                raise LocationError("storage_error")
            self.data = snapshot
            return deepcopy(snapshot)

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
