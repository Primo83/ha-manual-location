"""Observe selected Companion activity reports without changing person positions."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import logging
import time

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CoreState, callback, valid_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.storage import Store

from .const import DOMAIN, MAX_PERSONS
from .coordinator import LocationError, timestamp

_LOGGER = logging.getLogger(__name__)
CACHE_KEY = f"{DOMAIN}_phone_activity"
# Native Companion dispatcher protocol; importing mobile_app eagerly loads unrelated integrations.
SIGNAL_SENSOR_UPDATE = "mobile_app_sensor_update"
DEFAULT = {"enabled": True, "signals": ["unlock", "walking"], "match": "any", "manual_since": None}
DEPENDENCIES = {"unlock": ("screen", "lock"), "walking": ("walking",), "screen": ("screen",)}
SENSORS = {
    "screen": ("binary_sensor", "is_interactive"),
    "lock": ("binary_sensor", "keyguard_locked"),
    "walking": ("sensor", "detected_activity"),
}
WALKING = frozenset({"walking", "on_foot", "running"})
ACTIVITIES = WALKING | {"still", "tilting", "in_vehicle", "on_bicycle"}
FRESH_SECONDS = 30 * 60
ACTIVE_SECONDS = 60 * 60


def validate_activity(hass, value, previous=None, *, check_entities=True):
    """Validate independent per-person presentation settings."""
    if not isinstance(value, dict) or len(value) > MAX_PERSONS:
        raise LocationError("invalid_input")
    result, now = {}, datetime.now(UTC)
    for entity_id, settings in value.items():
        if (not isinstance(entity_id, str) or not entity_id.startswith("person.") or not valid_entity_id(entity_id)
                or (check_entities and hass.states.get(entity_id) is None
                    and (previous is None or settings != previous.get(entity_id)))
                or not isinstance(settings, dict) or set(settings) != set(DEFAULT)
                or type(settings["enabled"]) is not bool
                or not isinstance(settings["signals"], list) or len(settings["signals"]) > 3
                or any(not isinstance(signal, str) or signal not in DEPENDENCIES for signal in settings["signals"])
                or len(set(settings["signals"])) != len(settings["signals"])
                or settings["match"] not in ("any", "all")):
            raise LocationError("invalid_input")
        since = settings["manual_since"]
        if since is not None:
            since = timestamp(since)
            if since > now:
                raise LocationError("invalid_input")
            since = since.isoformat()
        if settings["enabled"] and since is None and not settings["signals"]:
            raise LocationError("invalid_input")
        result[entity_id] = {**settings, "signals": list(settings["signals"]), "manual_since": since}
    return result


class PhoneActivity:
    """Keep positive receipt times private and independent of the position revision."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.store = Store(self.hass, 1, CACHE_KEY, private=True, atomic_writes=True)
        self.positive = {}
        self._healthy = True
        self._load_error = False
        self._version = self._saved_version = 0
        self._write_lock = asyncio.Lock()
        self._write_timer = self._write_task = None
        self._started = False
        self._generation = 0
        self._bindings = {}
        self._entries = {}
        self._observed = {}
        self._pending = {}
        self._handles = {}
        self._unsubscribers = []
        self._sensor_unsubscribers = []
        self._rebind_handle = None
        self._startup_cancel = None

    async def async_load(self):
        try:
            stored = await self.store.async_load()
            if stored is None:
                return
            if not isinstance(stored, dict) or set(stored) != {"positive"} or not isinstance(stored["positive"], dict):
                raise ValueError
            if len(stored["positive"]) > 100:
                raise ValueError
            now = datetime.now(UTC)
            for phone, values in stored["positive"].items():
                identity = json.loads(phone)
                if (not isinstance(identity, list) or len(identity) != 2
                        or any(not isinstance(part, str) or not part or len(part) > 128 for part in identity)
                        or not isinstance(values, dict) or not set(values) <= set(DEPENDENCIES)):
                    raise ValueError
                for value in values.values():
                    if timestamp(value) > now:
                        raise ValueError
            self.positive = deepcopy(stored["positive"])
        except Exception:
            self._load_error = True
            self._healthy = False
            _LOGGER.error("Phone activity cache unavailable")

    def config(self, entity_id):
        return self.coordinator.data["activity"].get(entity_id, DEFAULT)

    def start(self):
        if self._started or self._startup_cancel is not None:
            return
        if self.hass.state != CoreState.running:
            self._startup_cancel = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._ha_started)
            return
        self._started = True
        self._unsubscribers = [
            self.hass.bus.async_listen("entity_registry_updated", self._metadata_changed),
            self.hass.bus.async_listen("device_registry_updated", self._metadata_changed),
            self.hass.bus.async_listen("state_changed", self._state_changed, event_filter=self._state_filter),
        ]
        self.rebind()

    @callback
    def _ha_started(self, _event):
        self._startup_cancel = None
        self.start()

    @callback
    def _state_filter(self, data):
        entity_id = data["entity_id"]
        return entity_id.startswith(("person.", "device_tracker.")) or entity_id in self._entries

    @callback
    def _state_changed(self, event):
        entity_id = event.data["entity_id"]
        if entity_id in self._entries:
            state = event.data["new_state"]
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self._observed.pop(entity_id, None)
            return
        self._metadata_changed(event)

    @callback
    def _metadata_changed(self, _event):
        if self._started and self._rebind_handle is None:
            self._rebind_handle = self.hass.loop.call_soon(self._rebind_safely)

    @callback
    def _rebind_safely(self):
        self._rebind_handle = None
        self.rebind()

    def _phone(self, state, registry):
        trackers = state.attributes.get("device_trackers")
        if state.state == STATE_UNAVAILABLE or not isinstance(trackers, (list, tuple)):
            return None

        def identity(entity_id, *, available=True):
            if not isinstance(entity_id, str) or not entity_id.startswith("device_tracker."):
                return None
            entry = registry.async_get(entity_id)
            if entry is None or entry.platform != "mobile_app" or not entry.device_id:
                return None
            config_entry = self.hass.config_entries.async_get_entry(entry.config_entry_id) if entry.config_entry_id else None
            if config_entry is None or config_entry.domain != "mobile_app":
                return None
            if available:
                loaded = self.hass.states.get(entity_id)
                if (entry.disabled_by is not None or config_entry.disabled_by is not None
                        or loaded is None or loaded.state == STATE_UNAVAILABLE):
                    return None
            return entry.config_entry_id, entry.device_id

        current = state.attributes.get("source")
        if isinstance(current, str) and current in trackers:
            current_entry = registry.async_get(current)
            if current_entry is not None and current_entry.platform == "mobile_app":
                # A known current phone must never be replaced by an old assigned phone.
                return identity(current)
        # Count registered phones before checking liveness; outages cannot disambiguate ownership.
        phones = {phone for tracker in trackers if (phone := identity(tracker, available=False)) is not None}
        if len(phones) != 1:
            return None
        phone = phones.pop()
        return phone if any(identity(tracker) == phone for tracker in trackers) else None

    def rebind(self):
        if not self._started:
            return
        try:
            self._bind()
        except Exception:
            self._clear_sensors()
            self._bindings = {}
            _LOGGER.error("Phone activity binding unavailable")

    def _bind(self):
        registry = er.async_get(self.hass)
        bindings, entries = {}, {}
        for entity_id in self.hass.states.async_entity_ids("person"):
            config = self.config(entity_id)
            if not config["enabled"] or config["manual_since"] is not None or not config["signals"]:
                continue
            phone = self._phone(self.hass.states.get(entity_id), registry)
            if phone is None:
                continue
            needed = {kind for signal in config["signals"] for kind in DEPENDENCIES[signal]}
            sensors, ambiguous = {}, False
            candidates = er.async_entries_for_config_entry(registry, phone[0])
            for kind in needed:
                domain, sensor_id = SENSORS[kind]
                matches = [entry for entry in candidates if entry.platform == "mobile_app"
                           and entry.device_id == phone[1] and entry.domain == domain
                           and isinstance(entry.unique_id, str) and entry.unique_id.endswith("_" + sensor_id)]
                if len(matches) > 1:
                    ambiguous = True
                    break
                if matches:
                    sensors[kind] = matches[0]
            if ambiguous:
                continue
            bindings[entity_id] = (phone, sensors, tuple(config["signals"]))
            entries.update({entry.entity_id: entry for entry in sensors.values() if entry.disabled_by is None})
        if bindings == self._bindings and entries == self._entries:
            return
        observed = {entity_id: value for entity_id, value in self._observed.items()
                    if entity_id in entries and self._entries.get(entity_id) == entries[entity_id]}
        self._clear_sensors()
        self._bindings, self._entries, self._observed = bindings, entries, observed
        generation = self._generation
        for entity_id, entry in entries.items():
            @callback
            def received(*_args, entity_id=entity_id, generation=generation):
                try:
                    if not self._started or generation != self._generation:
                        return
                    self._pending[entity_id] = (datetime.now(UTC), time.monotonic())
                    if entity_id not in self._handles:
                        self._handles[entity_id] = self.hass.loop.call_soon(self._consume_safely, entity_id, generation)
                except Exception:
                    self._observed.pop(entity_id, None)
                    _LOGGER.error("Phone activity report unavailable")
            signal = f"{SIGNAL_SENSOR_UPDATE}-{entry.domain}-{entry.unique_id}"
            self._sensor_unsubscribers.append(async_dispatcher_connect(self.hass, signal, received))

    def _clear_sensors(self):
        self._generation += 1
        for unsubscribe in self._sensor_unsubscribers:
            unsubscribe()
        self._sensor_unsubscribers = []
        for handle in self._handles.values():
            handle.cancel()
        self._handles = {}
        self._pending = {}
        self._observed = {}
        self._entries = {}

    @callback
    def _consume_safely(self, entity_id, generation):
        try:
            if not self._started or generation != self._generation:
                return
            self._handles.pop(entity_id, None)
            received_at, monotonic = self._pending.pop(entity_id)
            self.rebind()
            if generation != self._generation:
                return
            state = self.hass.states.get(entity_id)
            if state is None:
                self._observed.pop(entity_id, None)
                return
            value = state.state
            entry = self._entries[entity_id]
            valid = value in ("on", "off") if entry.domain == "binary_sensor" else value in ACTIVITIES
            if not valid:
                self._observed.pop(entity_id, None)
                return
            self._observed[entity_id] = (value, monotonic, received_at)
            current_monotonic = time.monotonic()
            for person_id, (phone, sensors, signals) in self._bindings.items():
                if not any(sensor.entity_id == entity_id for sensor in sensors.values()):
                    continue
                for signal in signals:
                    if (any(sensors.get(kind) and sensors[kind].entity_id == entity_id for kind in DEPENDENCIES[signal])
                            and self._fresh(signal, sensors, current_monotonic) and self._positive(signal, sensors)):
                        key = json.dumps(phone, separators=(",", ":"))
                        if key not in self.positive and len(self.positive) >= 100:
                            oldest = min(self.positive, key=lambda item: max(self.positive[item].values(), default=""))
                            self.positive.pop(oldest)
                        positive_at = max(self._observed[sensors[kind].entity_id][2] for kind in DEPENDENCIES[signal])
                        self.positive.setdefault(key, {})[signal] = positive_at.isoformat()
                        self._version += 1
            if self._version != self._saved_version:
                self._schedule_flush()
        except Exception:
            self._observed.pop(entity_id, None)
            _LOGGER.error("Phone activity report unavailable")

    def _fresh(self, signal, sensors, monotonic):
        for kind in DEPENDENCIES[signal]:
            entry = sensors.get(kind)
            if entry is None or entry.disabled_by is not None:
                return False
            observed = self._observed.get(entry.entity_id)
            state = self.hass.states.get(entry.entity_id)
            if (observed is None or state is None or state.state != observed[0]
                    or not 0 <= monotonic - observed[1] <= FRESH_SECONDS):
                return False
        return True

    def _positive(self, signal, sensors):
        values = {kind: self._observed[sensors[kind].entity_id][0] for kind in DEPENDENCIES[signal]}
        if signal == "unlock":
            return values["screen"] == "on" and values["lock"] == "off"
        if signal == "screen":
            return values["screen"] == "on"
        return values["walking"] in WALKING

    def summary(self, user, entity_id):
        unknown = {"status": "unknown", "timestamp": None}
        if user is None or not user.permissions.check_entity(entity_id, POLICY_READ):
            return unknown
        config = self.config(entity_id)
        if not config["enabled"]:
            return {"status": "hidden", "timestamp": None}
        if config["manual_since"] is not None:
            return {"status": "red", "timestamp": config["manual_since"]}
        if not self._healthy:
            return unknown
        self.rebind()
        binding = self._bindings.get(entity_id)
        if binding is None:
            return unknown
        phone, sensors, signals = binding
        if any(not user.permissions.check_entity(entry.entity_id, POLICY_READ) for entry in sensors.values()):
            return unknown
        now, monotonic = datetime.now(UTC), time.monotonic()
        values = self.positive.get(json.dumps(phone, separators=(",", ":")), {})
        times = {signal: timestamp(values[signal]) for signal in signals if signal in values}
        if any(value > now for value in times.values()):
            return unknown
        fresh = {signal: self._fresh(signal, sensors, monotonic) for signal in signals}
        if config["match"] == "all":
            if len(times) != len(signals) or not all(fresh.values()):
                return unknown
            last = min(times.values())
        else:
            if not times:
                return unknown
            last = max(times.values())
            green = any(fresh[signal] and (now - value).total_seconds() <= ACTIVE_SECONDS for signal, value in times.items())
            if not green and (not all(fresh.values()) or (now - last).total_seconds() <= ACTIVE_SECONDS):
                return unknown
        return {"status": "green" if (now - last).total_seconds() <= ACTIVE_SECONDS else "red",
                "timestamp": last.isoformat()}

    def _schedule_flush(self):
        if self._started and not self._load_error and self._write_timer is None:
            self._write_timer = self.hass.loop.call_later(5, self._launch_flush)

    @callback
    def _launch_flush(self):
        self._write_timer = None
        if self._write_task is None or self._write_task.done():
            self._write_task = self.hass.async_create_task(self.async_flush(), "Phone activity cache write")

    async def async_flush(self):
        async with self._write_lock:
            if self._load_error:
                return False
            if self._saved_version == self._version:
                return self._healthy
            version = self._version
            snapshot = {"positive": deepcopy(self.positive)}
            try:
                await self.store.async_save(snapshot)
                if await self.store.async_load() != snapshot:
                    raise ValueError
            except Exception:
                if self._healthy:
                    _LOGGER.error("Phone activity cache unavailable")
                self._healthy = False
                if self._version != version:
                    self._schedule_flush()
                return False
            self._healthy = True
            self._saved_version = version
            if self._saved_version != self._version:
                self._schedule_flush()
            return True

    async def async_shutdown(self):
        self._started = False
        if self._startup_cancel:
            self._startup_cancel()
            self._startup_cancel = None
        if self._rebind_handle:
            self._rebind_handle.cancel()
            self._rebind_handle = None
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers = []
        self._clear_sensors()
        self._bindings = {}
        if self._write_timer:
            self._write_timer.cancel()
            self._write_timer = None
        if self._write_task:
            await self._write_task
        await self.async_flush()
