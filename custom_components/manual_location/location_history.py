"""Join approved phone events with own points; keep native history PIN gated."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import hashlib
import math
from pathlib import Path
import secrets
import time

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.helpers import entity_registry as er

from .coordinator import LocationError, timestamp
from .gateway_bridge import GatewayError

MAX_VIEW_BYTES = 8 * 1024 * 1024


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _snapshot_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class LocationHistory:
    """The gateway callback never takes the coordinator's configuration lock."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.db = None
        self.tracker_id = None
        self._prepare_lock = asyncio.Lock()
        self.grants = {}
        self.active_grant = None
        self.previews = {}
        self._closing = False

    def stop(self):
        """Revoke callbacks before unloading waits for any other writer."""
        self._closing = True
        self.close_confirmed()
        self.previews.clear()

    async def async_shutdown(self):
        self.stop()
        async with self._prepare_lock:
            if self.db is not None:
                await self.db.async_close()

    def _tracker(self):
        bridge = self.coordinator.gateway
        state = self.hass.states.get(bridge.person_id)
        assigned = state.attributes.get("device_trackers", ()) if state else ()
        registry = er.async_get(self.hass)
        candidates = []
        for entity_id in assigned if isinstance(assigned, (list, tuple)) else ():
            entry = registry.async_get(entity_id)
            if (entry is not None and entry.platform == "mobile_app" and entry.disabled_by is None
                    and self.hass.states.get(entity_id) is not None):
                candidates.append(entity_id)
        if len(candidates) != 1:
            raise LocationError("history_unavailable")
        return candidates[0]

    async def async_prepare(self):
        from .gps_history import GpsHistory

        async with self._prepare_lock:
            if self._closing:
                raise LocationError("history_unavailable")
            tracker = self._tracker()
            if self.db is None:
                self.db = GpsHistory(self.hass, self.coordinator.gateway.person_id, tracker)
                await self.db.async_initialize()
                self.tracker_id = tracker
            if tracker != self.tracker_id or not self.db.available:
                raise LocationError("history_unavailable")
        return self.db

    async def async_ready(self):
        try:
            db = await self.async_prepare()
            return db.available and not db.pending
        except Exception:
            return False

    def close_started(self):
        now = time.time()
        for grant in self.grants.values():
            if grant["closing_at"] is None:
                grant.update(closing_at=now, deadline=time.monotonic() + 30)
        self.active_grant = None
        self.previews.clear()

    def close_confirmed(self):
        self.grants.clear()
        self.active_grant = None

    async def begin_normal(self, key):
        if not await self.async_ready():
            raise GatewayError("history unavailable")
        self.grants[key] = {"session": self.coordinator.gateway.session,
                            "closing_at": None, "deadline": None}
        self.active_grant = key

    def _grant_valid(self, payload):
        grant = self.grants.get(payload["grant"])
        return bool(grant and grant["session"] == payload["source_session"]
                    and (grant["closing_at"] is None or (
                        time.monotonic() <= grant["deadline"] and payload["lower"] <= grant["closing_at"])))

    def _snapshots(self):
        snapshots = {}
        for entity_id in (self.coordinator.gateway.person_id, self.tracker_id):
            state = self.hass.states.get(entity_id)
            if state is None:
                raise LocationError("history_unavailable")
            attrs = state.attributes
            snapshots[entity_id] = {
                "last_updated_ts": state.last_updated_timestamp,
                "last_updated": state.last_updated.isoformat(), "context_id": state.context.id,
                "state": state.state,
                **{key: attrs.get(key) for key in ("latitude", "longitude", "gps_accuracy")},
            }
        return snapshots

    async def async_receive(self, payload):
        required = {"operation_id", "grant", "source_session", "point", "snapshots", "lower", "upper", "elapsed"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise LocationError("invalid_input")
        for key in ("operation_id", "grant", "source_session"):
            if not isinstance(payload[key], str) or not 1 <= len(payload[key]) <= 128:
                raise LocationError("invalid_input")
        if (not all(_number(payload[key]) for key in ("lower", "upper", "elapsed"))
                or not 0 <= payload["elapsed"] <= 60 or payload["upper"] < payload["lower"]
                or abs(payload["upper"] - payload["lower"] - payload["elapsed"]) > 1
                or payload["upper"] > time.time() + 1 or payload["lower"] < time.time() - 90
                or not self._grant_valid(payload)):
            raise LocationError("history_unavailable")
        point = payload["point"]
        if (not isinstance(point, dict) or set(point) != {"person_id", "latitude", "longitude", "gps_accuracy"}
                or point["person_id"] != self.coordinator.gateway.person_id
                or not all(_number(point[key]) for key in ("latitude", "longitude", "gps_accuracy"))
                or abs(point["latitude"]) > 90 or abs(point["longitude"]) > 180 or point["gps_accuracy"] < 0):
            raise LocationError("invalid_input")
        db = await self.async_prepare()
        if not self.coordinator.loaded or db.pending or not self._grant_valid(payload):
            raise LocationError("history_unavailable")
        states = self._snapshots()
        if not isinstance(payload["snapshots"], dict) or set(payload["snapshots"]) != set(states):
            raise LocationError("invalid_input")
        for entity_id, state in states.items():
            supplied = payload["snapshots"][entity_id]
            if (not isinstance(supplied, dict) or set(supplied) != {"last_updated", "context_id"}
                    or not isinstance(supplied["last_updated"], str)
                    or timestamp(supplied["last_updated"]) != timestamp(state["last_updated"])
                    or supplied["context_id"] != state["context_id"]
                    or any(not _number(state[key]) or abs(state[key] - point[key]) >= 1e-8
                           for key in ("latitude", "longitude"))
                    or state["gps_accuracy"] != point["gps_accuracy"]):
                raise LocationError("history_unavailable")
        person = states[self.coordinator.gateway.person_id]
        accepted = {key: person[key] for key in ("latitude", "longitude")}
        accepted["timestamp"] = person["last_updated"]
        result = await db.async_record(payload["operation_id"], payload["grant"], accepted, states)
        # ACK preserves the exact REST representation bound by the producer.
        return {"history_id": result["history_id"],
                "native_timestamp": payload["snapshots"][self.coordinator.gateway.person_id]["last_updated"]}

    async def async_view(self, response, entity_ids, start, end):
        coordinator, bridge = self.coordinator, self.coordinator.gateway
        entity_id = bridge.person_id
        if entity_id not in entity_ids:
            return response
        revision, session = coordinator.data["revision"], bridge.session
        person = deepcopy(coordinator.data["persons"].get(entity_id))
        if not person or not person.get("current"):
            raise LocationError("history_unavailable")
        own = person["current"]
        points = list(person["history"])
        current = own
        db_revision = None
        if self.db is not None and self.db.available:
            db_revision = self.db.revision
            points = await self.db.async_points(start, end) + points
            try:
                status = await bridge.status()
                receipt = status["last_phone_receipt"]
                if (status["mode"] == "normal" and not bridge.pending and bridge.ready
                        and receipt and receipt.get("grant") == self.active_grant and receipt.get("history_id")):
                    record = await self.db.async_get(receipt["history_id"])
                    if record and record["grant"] == self.active_grant:
                        actual = self._snapshots()
                        if all(actual[key] == value for key, value in record["snapshots"].items()):
                            current = record["point"]
            except GatewayError:
                pass  # Safe own point remains available during gate downtime.
        points.append(own)
        start_at, end_at = timestamp(start), timestamp(end)
        by_time = {timestamp(point["timestamp"]): point for point in points}
        history = [point for instant, point in sorted(by_time.items()) if start_at <= instant <= end_at]
        preceding = [instant for instant in by_time if instant < start_at]
        if preceding:
            history.insert(0, by_time[max(preceding)])
        if (revision != coordinator.data["revision"] or session != bridge.session
                or (db_revision is not None and db_revision != self.db.revision)):
            raise LocationError("history_changed")
        response["overrides"][entity_id] = {"current": deepcopy(current), "history": deepcopy(history)}
        if len(json.dumps(response, separators=(",", ":")).encode()) > MAX_VIEW_BYTES:
            raise LocationError("history_too_large")
        return response

    def _authorize(self, connection, token, entity_id, revision=None):
        self.coordinator.require_session(connection, token)
        if entity_id != self.coordinator.gateway.person_id:
            raise LocationError("unauthorized")
        for item in (entity_id, self.tracker_id):
            if item and not connection.user.permissions.check_entity(item, POLICY_READ):
                raise LocationError("unauthorized")
        if revision is not None:
            self.coordinator._require_revision(revision)

    async def async_page(self, connection, token, entity_id, start, end, cursor):
        self._authorize(connection, token, entity_id)
        start_at, end_at = timestamp(start), timestamp(end)
        if end_at < start_at or end_at - start_at > timedelta(days=31):
            raise LocationError("invalid_input")
        revision = self.coordinator.data["revision"]
        db = await self.async_prepare()
        self._authorize(connection, token, entity_id)
        result = await db.async_panel_page(start, end, cursor, 100)
        self._authorize(connection, token, entity_id, revision)
        return {**result, "history_revision": result["revision"], "revision": revision, "supported": True}

    async def _custom_snapshots(self):
        coordinator, bridge = self.coordinator, self.coordinator.gateway
        status = await bridge.status()
        person = coordinator.data["persons"].get(bridge.person_id)
        own = person.get("current") if person else None
        if (not own or not coordinator.data["enabled"] or not person["enabled"]
                or not bridge.ready or bridge.pending or status["mode"] != "custom"
                or not status["gps_blocked"] or not status["last_custom_receipt"]):
            raise LocationError("history_requires_custom")
        states = self._snapshots()
        if any(state["gps_accuracy"] != 0 or any(
            not _number(state[key]) or abs(state[key] - own[key]) >= 1e-8
            for key in ("latitude", "longitude")
        ) for state in states.values()):
            raise LocationError("history_requires_custom")

        def restore_matches():
            try:
                records = json.loads(Path(self.hass.config.path(".storage", "core.restore_state")).read_text())["data"]
                for entity_id in states:
                    record = next(item for item in records if item.get("state", {}).get("entity_id") == entity_id)
                    attrs = record["state"]["attributes"]
                    if attrs.get("gps_accuracy") != 0 or any(
                        not _number(attrs.get(key)) or abs(attrs[key] - own[key]) >= 1e-8
                        for key in ("latitude", "longitude")
                    ):
                        return False
                    if entity_id == self.tracker_id:
                        extra = record.get("extra_data", {}).get("data", {})
                        if extra.get("gps_accuracy") != 0 or extra.get("gps") != [own["latitude"], own["longitude"]]:
                            return False
                return True
            except (OSError, ValueError, KeyError, StopIteration, TypeError):
                return False
        if not await self.hass.async_add_executor_job(restore_matches):
            raise LocationError("history_requires_custom")
        return states

    async def async_preview(self, connection, token, entity_id, revision, selection=None, *, start=None, end=None, include_custom=False):
        async with self.coordinator._save_lock:
            self._authorize(connection, token, entity_id, revision)
            db = await self.async_prepare()
            states = await self._custom_snapshots()
            self._authorize(connection, token, entity_id, revision)
            if type(include_custom) is not bool:
                raise LocationError("invalid_input")
            own_plan = None
            removed = []
            if start is not None:
                a, b = timestamp(start), timestamp(end)
                if b < a or b - a > timedelta(days=31):
                    raise LocationError("invalid_input")
                preview = await db.async_range_preview(start, end, states, allow_empty=include_custom, allow_current=True)
                if include_custom:
                    target = deepcopy(self.coordinator.data)
                    person = target["persons"][entity_id]
                    removed = [point for point in person["history"] if a <= timestamp(point["timestamp"]) <= b]
                    if removed:
                        person["history"] = [point for point in person["history"] if not a <= timestamp(point["timestamp"]) <= b]
                        target["revision"] += 1
                        own_plan = {"before_hash": _snapshot_hash(self.coordinator.data),
                                    "after_hash": _snapshot_hash(target), "target_snapshot": target}
            else:
                if not isinstance(selection, list) or not 1 <= len(selection) <= 100:
                    raise LocationError("invalid_input")
                gps_ids, native_ids = [], []
                for row in selection:
                    if not isinstance(row, dict) or set(row) != {"kind", "ref"} or not isinstance(row["ref"], str):
                        raise LocationError("invalid_input")
                    if row["kind"] == "accepted" and row["ref"].startswith("accepted:"):
                        gps_ids.append(row["ref"].removeprefix("accepted:"))
                    elif row["kind"] == "native" and row["ref"].startswith("native:") and row["ref"][7:].isdigit():
                        native_ids.append(int(row["ref"][7:]))
                    else:
                        raise LocationError("invalid_input")
                preview = await db.async_preview(gps_ids, native_ids, states)
            self._authorize(connection, token, entity_id, revision)
            if not removed and not preview["counts"].get("native") and not preview["counts"].get("accepted"):
                raise LocationError("history_empty_selection")
            operation_id = secrets.token_hex(16)
            self.previews = {preview["token"]: {"connection": connection, "token": token,
                             "operation_id": operation_id, "revision": revision,
                             "session": self.coordinator.gateway.session,
                             "own_plan": own_plan, "custom_count": len(removed),
                             "expires": time.monotonic() + 120}}
            result = {**preview, "preview_token": preview["token"], "operation_id": operation_id}
            result["counts"] = {**preview["counts"], "custom": len(removed)}
            if removed:
                result["rows"] = (preview.get("rows", []) + [
                    {"kind": "custom", "ref": point["timestamp"], "entity_id": entity_id,
                     "point": point, "timestamp": point["timestamp"]} for point in removed
                ])[:100]
                result["truncated"] = len(removed) + len(preview.get("rows", [])) > 100 or preview.get("truncated", False)
            result["current_preserved"] = True
            if start is not None:
                result["selected_range"] = {"start": start, "end": end}
            return result

    async def async_execute(self, connection, token, entity_id, revision, preview_token, operation_id):
        async with self.coordinator._save_lock:
            self._authorize(connection, token, entity_id, revision)
            preview = self.previews.pop(preview_token, None)
            if (not preview or preview["connection"] is not connection or preview["token"] != token
                    or preview["operation_id"] != operation_id or preview["revision"] != revision
                    or preview["session"] != self.coordinator.gateway.session or time.monotonic() > preview["expires"]):
                raise LocationError("history_changed")
            await self._custom_snapshots()
            self._authorize(connection, token, entity_id, revision)
            if preview["own_plan"] is not None:
                await self._check_own_plan(preview["own_plan"])
            try:
                result = await self.db.async_execute(preview_token, operation_id,
                                                    own_plan=preview["own_plan"], own_commit=self._commit_own)
            except BaseException:
                if self.db.pending:
                    self.coordinator._schedule_gateway_retry()
                raise
            self._authorize(connection, token, entity_id)
            return {**result, "deleted": True, "operation_id": operation_id,
                    "counts": {**result.get("counts", {}), "custom": preview["custom_count"]}}

    async def _check_own_plan(self, plan):
        durable = await self.coordinator.store.async_load()
        current_hash = _snapshot_hash(durable)
        if (current_hash not in {plan["before_hash"], plan["after_hash"]}
                or _snapshot_hash(self.coordinator.data) not in {plan["before_hash"], plan["after_hash"]}):
            raise LocationError("history_changed")
        return durable, current_hash

    async def _commit_own(self, plan):
        durable, current_hash = await self._check_own_plan(plan)
        if current_hash == plan["after_hash"]:
            self.coordinator.data = deepcopy(durable)
            return
        target = plan["target_snapshot"]
        if (self.coordinator._gateway_current(target) != self.coordinator._gateway_current(self.coordinator.data)
                or not self.coordinator.gateway.ready or self.coordinator.gateway.pending):
            raise LocationError("history_requires_custom")
        # The caller already owns _save_lock, before the history DB lock.
        # Write only the exact journalled snapshot: no route/epoch/Gate effects.
        await self.coordinator.store.async_save(deepcopy(target))
        if await self.coordinator.store.async_load() != target:
            raise LocationError("storage_error")
        self.coordinator._require_loaded()
        self.coordinator.data = deepcopy(target)

    async def async_recover(self):
        if self.db is not None and self.db.pending:
            states = await self._custom_snapshots()
            if not await self.db.async_recover(states, own_commit=self._commit_own):
                raise LocationError("history_unavailable")
