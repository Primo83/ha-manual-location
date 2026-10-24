"""Join approved phone events with own points; keep native history PIN gated."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import hashlib
import hmac
import math
from pathlib import Path
import re
import secrets
import sys
import time

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.helpers import entity_registry as er

from .coordinator import LocationError, timestamp
from .gateway_bridge import GatewayError

MAX_VIEW_BYTES = 8 * 1024 * 1024
SNAPSHOT_SECONDS = 300
SNAPSHOT_MEMORY = 32 * 1024 * 1024


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _snapshot_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class LocationHistory:
    """Phone admission bypasses the configuration lock; correction callbacks own it."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.db = None
        self.tracker_id = None
        self._prepare_lock = asyncio.Lock()
        self.grants = {}
        self.active_grant = None
        self.previews = {}
        self._correction_requests = {}
        self._correction_task = None
        self._closing = False
        self._snapshots_cache = {}
        self._snapshot_timer = None
        self._selection_key = secrets.token_bytes(32)

    @property
    def correction_active(self):
        return bool(self._correction_requests or (self.db is not None and self.db.pending)
                    or getattr(self.coordinator.gateway, "history_correction_pending", False))

    def stop(self):
        """Revoke callbacks before unloading waits for any other writer."""
        self._closing = True
        self.close_confirmed()
        self.previews.clear()
        self._clear_snapshots()

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

    def close_started(self, *, cutoff=None):
        now = time.time() if cutoff is None else cutoff
        for grant in self.grants.values():
            if grant["closing_at"] is None or now < grant["closing_at"]:
                grant.update(closing_at=now, deadline=time.monotonic() + 30, strict_cutoff=cutoff is not None)
        self.active_grant = None
        self.previews.clear()

    def close_confirmed(self):
        self.grants.clear()
        self.active_grant = None

    async def begin_normal(self, key):
        if not await self.async_ready():
            raise GatewayError("history unavailable")
        self.grants[key] = {"session": self.coordinator.gateway.session,
                            "closing_at": None, "deadline": None,
                            "generation": await self.db.async_generation() if self.db is not None else 0}
        self.active_grant = key

    def _grant_valid(self, payload):
        grant = self.grants.get(payload["grant"])
        return bool(grant and grant["session"] == payload["source_session"]
                    and (grant["closing_at"] is None or (
                        time.monotonic() <= grant["deadline"] and (
                            payload["lower"] < grant["closing_at"] if grant.get("strict_cutoff")
                            else payload["lower"] <= grant["closing_at"]))))

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
        if not isinstance(payload, dict) or set(payload) not in (required, required | {"core_boot"}):
            raise LocationError("invalid_input")
        core_boot = payload.get("core_boot")
        if core_boot is not None and core_boot != self.hass.data.get("manual_location_core_boot"):
            raise LocationError("history_unavailable")
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
        if not self.coordinator.loaded or not self._grant_valid(payload):
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
        admission = None if core_boot is None else {
            "request_digest": _snapshot_hash(payload), "source_session": payload["source_session"], "core_boot": core_boot}
        # A late admitted callback may complete history, but cannot displace a
        # later explicit Own intent. Closed grants retain their admission only
        # while no own-current fence has consumed them.
        # Queue behind an active exact deletion; a durable pending failure still
        # rejects capture inside the DB lock, without dropping a healthy live report.
        result = await db.async_record(payload["operation_id"], payload["grant"], accepted, states,
                                       admission=admission, promote=self.grants[payload["grant"]]["generation"])
        # ACK preserves the exact REST representation bound by the producer.
        return {"history_id": result["history_id"],
                "native_timestamp": payload["snapshots"][self.coordinator.gateway.person_id]["last_updated"]}

    async def async_query(self, binding):
        required = {"operation_id", "grant", "source_session", "core_boot", "request_digest"}
        if (not isinstance(binding, dict) or set(binding) != required
                or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in binding.values())):
            raise LocationError("invalid_input")
        db = await self.async_prepare()
        record = await db.async_operation(binding)
        return {"found": False} if record is None else {"found": True, **record}

    async def async_current(self):
        """Use acceptance order, never raw native state or Native timestamps."""
        person = self.coordinator.data["persons"].get(self.coordinator.gateway.person_id)
        if person and person["enabled"]:
            return deepcopy(person["current"])
        if self.db is not None and self.db.available:
            record = await self.db.async_operation()
            if record is not None:
                return deepcopy(record["point"])
        return deepcopy(person.get("current")) if person else None

    async def async_own_current(self):
        self.close_confirmed()
        if self.db is not None:
            await self.db.async_own_current()

    async def async_preserve_handoff(self, handoff_id, point):
        """Keep a timestamp tie as a verified HA publication, without changing 1.x history IDs."""
        db = await self.async_prepare()
        states = self._snapshots()
        if any(state["gps_accuracy"] != 0 or any(
                not _number(state[key]) or abs(state[key] - point[key]) >= 1e-8
                for key in ("latitude", "longitude")) for state in states.values()):
            raise LocationError("history_unavailable")
        await db.async_record("own_handoff_" + handoff_id, "own_handoff", point, states, own_replay=True)

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
        current = await self.async_current()
        db_revision = None
        if self.db is not None and self.db.available:
            db_revision = self.db.revision
            points = await self.db.async_points(start, end) + points
            try:
                status = await bridge.status()
                receipt = status["last_phone_receipt"]
                if (current == own and status["mode"] == "normal" and not bridge.pending and bridge.ready
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
        # Equal timestamps can belong to different events/sources. Coalesce only
        # the exact same display point (e.g. the frozen phone becomes Own).
        unique = {(_snapshot_hash(point)): point for point in points}
        ordered = sorted(unique.values(), key=lambda point: (timestamp(point["timestamp"]), _snapshot_hash(point)))
        history = [point for point in ordered if start_at <= timestamp(point["timestamp"]) <= end_at]
        preceding = [point for point in ordered if timestamp(point["timestamp"]) < start_at]
        if preceding:
            history.insert(0, preceding[-1])
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
        try:
            parsed = json.loads(cursor) if isinstance(cursor, str) else None
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and "snapshot_version" in parsed:
            result = await self._snapshot_page(connection, token, entity_id, start, end, parsed, revision, db)
        else:
            # Existing clients retain the original fresh-range hash contract.
            result = await db.async_panel_page(start, end, cursor, 100)
        self._authorize(connection, token, entity_id, revision)
        return {**result, "history_revision": result["revision"], "revision": revision, "supported": True}

    def _clear_snapshots(self):
        self._snapshots_cache.clear()
        if self._snapshot_timer:
            self._snapshot_timer.cancel()
            self._snapshot_timer = None

    def _expire_snapshots(self):
        if self._snapshot_timer:
            self._snapshot_timer.cancel()
            self._snapshot_timer = None
        now = time.monotonic()
        self._snapshots_cache = {key: entry for key, entry in self._snapshots_cache.items()
                                 if entry["expires"] > now}
        if self._snapshots_cache and not self._closing:
            delay = max(0.01, min(entry["expires"] for entry in self._snapshots_cache.values()) - now)
            self._snapshot_timer = self.hass.loop.call_later(delay, self._expire_snapshots)

    def _selection_proof(self, connection, token, entity_id, revision, snapshot_id, ref, fingerprint):
        payload = json.dumps([id(connection), token, entity_id, revision, snapshot_id, ref, fingerprint],
                             separators=(",", ":")).encode()
        return hmac.new(self._selection_key, payload, hashlib.sha256).hexdigest()

    async def _snapshot_page(self, connection, token, entity_id, start, end, cursor, revision, db):
        if type(cursor.get("snapshot_version")) is not int or cursor["snapshot_version"] != 1:
            raise LocationError("history_changed")
        self._expire_snapshots()
        scope = (connection, token, entity_id, self.tracker_id, start, end, revision)
        offset = 0
        if set(cursor) == {"snapshot_version"}:
            rows, signature, history_revision = await db.async_panel_snapshot(start, end)
            self._authorize(connection, token, entity_id, revision)
            snapshot_id = secrets.token_urlsafe(24)
            entry = {"scope": scope, "rows": rows, "signature": signature, "revision": history_revision,
                     "delete_epoch": db.delete_epoch,
                     "captured_at": datetime.now(UTC).isoformat(), "expires": time.monotonic() + SNAPSHOT_SECONDS}
            # Count the actual compact objects, including the container and metadata.
            entry["bytes"] = (sys.getsizeof(rows) + sum(map(sys.getsizeof, rows))
                              + sys.getsizeof(entry) + sys.getsizeof(scope)
                              + sum(sys.getsizeof(value) for value in scope)
                              + sum(sys.getsizeof(value) for key, value in entry.items() if key not in {"rows", "scope"})
                              + sys.getsizeof(snapshot_id) + 1024)
            if entry["bytes"] > SNAPSHOT_MEMORY:
                raise LocationError("history_too_large")
            for key, previous in list(self._snapshots_cache.items()):
                if previous["scope"][0] is connection and previous["scope"][1] == token:
                    matching = [item for item in self._snapshots_cache.values()
                                if item["scope"][0] is connection and item["scope"][1] == token]
                    if len(matching) >= 2:
                        del self._snapshots_cache[key]
            while self._snapshots_cache and (len(self._snapshots_cache) >= 8
                    or sum(item["bytes"] for item in self._snapshots_cache.values()) + entry["bytes"] > SNAPSHOT_MEMORY):
                del self._snapshots_cache[next(iter(self._snapshots_cache))]
            self._snapshots_cache[snapshot_id] = entry
        elif set(cursor) == {"snapshot_version", "snapshot_id", "offset"}:
            snapshot_id, offset = cursor["snapshot_id"], cursor["offset"]
            if not isinstance(snapshot_id, str) or type(offset) is not int or offset < 100 or offset % 100:
                raise LocationError("history_changed")
            entry = self._snapshots_cache.get(snapshot_id)
            if (entry is None or entry["scope"] != scope or offset >= len(entry["rows"])
                    or db.pending or entry["delete_epoch"] != db.delete_epoch):
                raise LocationError("history_changed")
        else:
            raise LocationError("history_changed")
        entry["expires"] = time.monotonic() + SNAPSHOT_SECONDS
        self._expire_snapshots()
        selected = [json.loads(row) for row in entry["rows"][offset:offset + 100]]
        for row in selected:
            row["snapshot_id"] = snapshot_id
            row["proof"] = self._selection_proof(connection, token, entity_id, revision, snapshot_id,
                                                 row["ref"], row["fingerprint"])
        has_more = offset + 100 < len(entry["rows"])
        return {"rows": selected, "has_more": has_more, "revision": entry["revision"],
                "total_rows": len(entry["rows"]),
                "signature": entry["signature"], "snapshot_id": snapshot_id, "snapshot_version": 1,
                "captured_at": entry["captured_at"], "start": start, "end": end,
                "next_cursor": json.dumps({"snapshot_version": 1, "snapshot_id": snapshot_id,
                                           "offset": offset + 100}) if has_more else None}

    async def _delete_snapshots(self):
        """Allow confirmed live GPS without opening or closing its grant."""
        coordinator, bridge = self.coordinator, self.coordinator.gateway
        status = await bridge.status()
        person = coordinator.data["persons"].get(bridge.person_id)
        if status["mode"] == "normal":
            if (not person or person["enabled"] or not coordinator.data["enabled"]
                    or not bridge.ready or bridge.pending):
                raise LocationError("history_source_unconfirmed")
            return self._snapshots(), False
        if status["mode"] != "custom":
            raise LocationError("history_source_unconfirmed")
        return await self._custom_snapshots(status), True

    async def _custom_snapshots(self, status=None):
        coordinator, bridge = self.coordinator, self.coordinator.gateway
        status = status or await bridge.status()
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
            states, custom_mode = await self._delete_snapshots()
            self._authorize(connection, token, entity_id, revision)
            if type(include_custom) is not bool:
                raise LocationError("invalid_input")
            target = deepcopy(self.coordinator.data)
            person = target["persons"][entity_id]
            removed, gps_ids, native_ids, fingerprints = [], [], [], {}
            own_changed = False
            if start is not None:
                a, b = timestamp(start), timestamp(end)
                if b < a or b - a > timedelta(days=31):
                    raise LocationError("invalid_input")
                if include_custom:
                    removed = [point for point in person["history"] if a <= timestamp(point["timestamp"]) <= b]
                    person["history"] = [point for point in person["history"] if not a <= timestamp(point["timestamp"]) <= b]
                preview = await db.async_range_preview(start, end, states, allow_empty=include_custom, allow_current=True)
            else:
                if not isinstance(selection, list) or not 1 <= len(selection) <= 100:
                    raise LocationError("invalid_input")
                for row in selection:
                    if (not isinstance(row, dict) or set(row) not in (
                            {"kind", "ref"}, {"kind", "ref", "snapshot_id", "fingerprint", "proof"})
                            or not isinstance(row.get("ref"), str)):
                        raise LocationError("invalid_input")
                    if row["kind"] == "custom":
                        if set(row) != {"kind", "ref"} or own_changed or row["ref"] != person["current"]["timestamp"]:
                            raise LocationError("history_changed")
                        own_changed = True
                        removed.append(deepcopy(person["current"]))
                        person["history"] = [point for point in person["history"] if point["timestamp"] != row["ref"]]
                        continue
                    if "snapshot_id" in row:
                        if (any(not isinstance(row[key], str) or not row[key] or len(row[key]) > 256
                                for key in ("snapshot_id", "fingerprint", "proof"))
                                or any(re.fullmatch(r"[0-9a-f]{64}", row[key]) is None for key in ("fingerprint", "proof"))
                                or not hmac.compare_digest(row["proof"], self._selection_proof(
                                    connection, token, entity_id, revision, row["snapshot_id"], row["ref"], row["fingerprint"]))):
                            raise LocationError("history_changed")
                        fingerprints[row["ref"]] = row["fingerprint"]
                    if row["kind"] == "accepted" and row["ref"].startswith("accepted:"):
                        gps_ids.append(row["ref"].removeprefix("accepted:"))
                    elif row["kind"] == "native" and row["ref"].startswith("native:") and row["ref"][7:].isdigit():
                        native_ids.append(int(row["ref"][7:]))
                    else:
                        raise LocationError("invalid_input")
                if own_changed:
                    candidates = [point for point in person["history"] if timestamp(point["timestamp"]) <= datetime.now(UTC)]
                    accepted = await db.async_last_admitted(gps_ids)
                    replacement = max(candidates, key=lambda point: timestamp(point["timestamp"])) if candidates else (accepted["point"] if accepted else None)
                    if replacement is None:
                        raise LocationError("history_no_replacement")
                    person["current"] = deepcopy(replacement)
                preview = await db.async_preview(gps_ids, native_ids, states, fingerprints,
                                                allow_current=True, allow_empty=own_changed)
            own_plan = None
            if removed:
                for scenario in target.get("scenarios", {}).values():
                    if (scenario["entity_id"] == entity_id and scenario["status"] == "running"
                            and scenario["cursor"] > scenario.get("run_start_cursor", 0)):
                        scenario.update(status="stopped", reason="manual_change")
                target["revision"] += 1
                own_plan = {"before_hash": _snapshot_hash(self.coordinator.data),
                            "after_hash": _snapshot_hash(target), "target_snapshot": target}
            self._authorize(connection, token, entity_id, revision)
            if not removed and not preview["counts"].get("native") and not preview["counts"].get("accepted"):
                raise LocationError("history_empty_selection")
            correction = await db.async_correction_target(preview["token"], states, person["current"], own_changed,
                                                          "custom" if custom_mode else "normal")
            needs_correction = not correction["current_preserved"] or own_changed
            if needs_correction and (custom_mode or not self.coordinator.gateway.history_correction_supported):
                raise LocationError("history_correction_unavailable")
            operation_id = secrets.token_hex(16)
            self.previews = {preview["token"]: {"connection": connection, "token": token,
                             "operation_id": operation_id, "revision": revision,
                             "session": self.coordinator.gateway.session, "entity_id": entity_id,
                             "own_plan": own_plan, "custom_count": len(removed),
                             "correction": needs_correction, "own": person["current"], "own_changed": own_changed,
                             "mode": "custom" if custom_mode else "normal", "expires": time.monotonic() + 120}}
            result = {**preview, "preview_token": preview["token"], "operation_id": operation_id,
                      **{key: correction[key] for key in ("current_preserved", "current_replacement")}}
            result["counts"] = {**preview["counts"], "custom": len(removed)}
            if removed:
                result["rows"] = (preview.get("rows", []) + [
                    {"kind": "custom", "ref": point["timestamp"], "entity_id": entity_id,
                     "point": point, "timestamp": point["timestamp"]} for point in removed
                ])[:100]
                result["truncated"] = len(removed) + len(preview.get("rows", [])) > 100 or preview.get("truncated", False)
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
            _states, custom_mode = await self._delete_snapshots()
            self._authorize(connection, token, entity_id, revision)
            self._clear_snapshots()
            if preview["own_plan"] is not None:
                await self._check_own_plan(preview["own_plan"])
            if preview.get("correction"):
                if custom_mode or preview["mode"] != "normal":
                    raise LocationError("history_source_unconfirmed")
                if not self.coordinator.gateway.history_correction_supported:
                    raise LocationError("history_correction_unavailable")
                self._correction_requests[operation_id] = {**preview, "preview_token": preview_token}
            else:
                try:
                    result = await self.db.async_execute(preview_token, operation_id,
                                                        own_plan=preview["own_plan"], own_commit=self._commit_own, live=not custom_mode)
                except BaseException:
                    if self.db.pending:
                        self.coordinator._schedule_gateway_retry()
                    raise
                self._authorize(connection, token, entity_id)
                current = await self.async_current()
                admitted = await self.db.async_operation()
                return {**result, "deleted": True, "operation_id": operation_id,
                        "current_preserved": True, "current_replacement": {"point": current, "source": "ha" if admitted and not custom_mode else "custom"},
                        "counts": {**result.get("counts", {}), "custom": preview["custom_count"]}}
        # The Gateway owns its writer lock before calling back into our save/DB locks.
        return await self._run_correction(operation_id, preview_token)

    async def _run_correction(self, operation_id, preview_token):
        try:
            reply = await self.coordinator.gateway.correct_history(operation_id, preview_token)
            result = reply.get("result")
            if not isinstance(result, dict):
                raise LocationError("history_changed")
            if result.get("rejected"):
                self._correction_requests.pop(operation_id, None)
                raise LocationError(result.get("error_code", "history_changed"))
            if result.get("deleted") is not True or result.get("operation_id") != operation_id:
                raise LocationError("history_changed")
            self._correction_requests.pop(operation_id, None)
            return result
        except BaseException:
            self.coordinator._schedule_gateway_retry()
            raise

    async def async_correction(self, payload):
        required = {"phase", "operation_id", "preview_token", "source_session", "core_boot"}
        if (not isinstance(payload, dict) or set(payload) not in (required, required | {"plan_id"})
                or payload["phase"] not in {"prepare", "commit"}
                or (payload["phase"] == "commit" and "plan_id" not in payload)
                or any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in payload.values())):
            raise LocationError("invalid_input")
        operation_id, preview_token = payload["operation_id"], payload["preview_token"]
        async with self.coordinator._save_lock:
            db = await self.async_prepare()
            existing = await db.async_correction_state(operation_id, preview_token)
            if existing is not None:
                if "plan_id" in payload and existing["plan_id"] != payload["plan_id"]:
                    raise LocationError("history_changed")
                if payload["phase"] == "prepare" or "result" in existing:
                    return existing
            if payload["phase"] == "prepare":
                request = self._correction_requests.get(operation_id)
                try:
                    if (not request or request["preview_token"] != preview_token
                            or request["session"] != payload["source_session"]
                            or payload["source_session"] != self.coordinator.gateway.session
                            or payload["core_boot"] != self.hass.data.get("manual_location_core_boot")
                            or request["expires"] < time.monotonic()):
                        raise LocationError("history_changed")
                    self._authorize(request["connection"], request["token"], request["entity_id"])
                    if request["revision"] != self.coordinator.data["revision"]:
                        raise LocationError("revision_conflict")
                    if request["own_plan"] is not None:
                        await self._check_own_plan(request["own_plan"])
                except LocationError as error:
                    return {"operation_id": operation_id, "rejected": True, "error_code": error.code}
                from .gps_history import GpsHistoryError
                try:
                    return await db.async_prepare_correction(preview_token, operation_id, self._snapshots(),
                        own_plan=request["own_plan"], own=request["own"], own_changed=request["own_changed"],
                        mode=request["mode"], custom_count=request["custom_count"])
                except GpsHistoryError as error:
                    if db.available and not db.pending:
                        return {"operation_id": operation_id, "rejected": True, "error_code": error.code}
                    raise
            if existing is None or "plan_id" not in payload:
                raise LocationError("history_changed")
            async def commit_own(plan):
                await self._commit_own(plan, correction=True)
            response = await db.async_commit_correction(operation_id, preview_token, payload["plan_id"],
                                                        own_commit=commit_own, native_check=self._check_correction_native)
            # Keep reservation until the Gateway has durably installed its new baseline.
            return response

    def _check_correction_native(self, target):
        if target["kind"] == "unchanged":
            return
        point = target["point"]
        if any(any(not _number(state[key]) or abs(state[key] - point[key]) >= 1e-8
                   for key in ("latitude", "longitude", "gps_accuracy")) for state in self._snapshots().values()):
            raise LocationError("history_changed")

    async def _check_own_plan(self, plan):
        durable = await self.coordinator.store.async_load()
        current_hash = _snapshot_hash(durable)
        if (current_hash not in {plan["before_hash"], plan["after_hash"]}
                or _snapshot_hash(self.coordinator.data) not in {plan["before_hash"], plan["after_hash"]}):
            raise LocationError("history_changed")
        return durable, current_hash

    async def _commit_own(self, plan, *, correction=False):
        durable, current_hash = await self._check_own_plan(plan)
        if current_hash == plan["after_hash"]:
            self.coordinator.data = deepcopy(durable)
            return
        target = plan["target_snapshot"]
        if not correction and (self.coordinator._gateway_current(target) != self.coordinator._gateway_current(self.coordinator.data)
                or not self.coordinator.gateway.ready or self.coordinator.gateway.pending):
            raise LocationError("history_requires_custom")
        # The caller already owns _save_lock, before the history DB lock.
        # Write only the exact journalled snapshot: no route/epoch/Gate effects.
        await self.coordinator.store.async_save(deepcopy(target))
        if await self.coordinator.store.async_load() != target:
            raise LocationError("storage_error")
        self.coordinator._require_loaded()
        self.coordinator.data = deepcopy(target)
        if correction:
            self.coordinator._gateway_applied = deepcopy(self.coordinator._gateway_current(target))
            self.coordinator._reschedule()

    async def _retry_correction(self, operation_id, preview_token):
        try:
            await self._run_correction(operation_id, preview_token)
        except (LocationError, GatewayError):
            self.coordinator._schedule_gateway_retry()

    async def async_recover(self):
        if self._correction_requests:
            operation_id, request = next(iter(self._correction_requests.items()))
            if self._correction_task is None or self._correction_task.done():
                self._correction_task = self.hass.async_create_task(self._retry_correction(operation_id, request["preview_token"]))
            return
        if self.db is not None and self.db.pending:
            marker, _revision = await self.db._io(self.db._read_state)
            if marker and marker.get("correction"):
                if self._correction_task is None or self._correction_task.done():
                    self._correction_task = self.hass.async_create_task(self._retry_correction(marker["operation_id"], marker["token"]))
                return
            self._clear_snapshots()
            states, allow_current = await self._delete_snapshots()
            if not await self.db.async_recover(states, own_commit=self._commit_own, allow_current=allow_current):
                raise LocationError("history_unavailable")
        elif self.coordinator.gateway.history_correction_pending:
            # A Gate background retry may finish after this Core lost its WS context.
            # Status is nonblocking during the barrier; clear a stale cached busy flag.
            await self.coordinator.gateway.status()
