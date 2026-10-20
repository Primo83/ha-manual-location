"""Accepted phone history and exact, queued Recorder deletion.

The caller owns PIN, grants, custom/restore checks and the configuration lock.
This module never calls the gateway or takes the coordinator's save lock.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time

MAX_ROWS = 100_000
MAX_BYTES = 8 * 1024 * 1024
MAX_SELECTION = 1000
PREVIEW_SECONDS = 300


class GpsHistoryError(Exception):
    """An error code without locations, database paths or SQL."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _instant(value):
    if not isinstance(value, str):
        raise GpsHistoryError("history_invalid_input")
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError
        return result.timestamp()
    except (ValueError, OverflowError):
        raise GpsHistoryError("history_invalid_input") from None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _bounded(value):
    if len(_json(value).encode()) > MAX_BYTES:
        raise GpsHistoryError("history_too_large")
    return value


def _point(latitude, longitude, timestamp):
    if (not _number(latitude) or not _number(longitude)
            or abs(latitude) > 90 or abs(longitude) > 180):
        return None
    return {"latitude": latitude, "longitude": longitude, "timestamp": timestamp}


def _ref_key(ref):
    return ref["entity_id"], ref["last_updated_ts"], ref["context_id"]


class GpsHistory:
    """One private SQLite index; normal capture is independent of Recorder."""

    def __init__(self, hass, person_id, tracker_id):
        self.hass, self.person_id, self.tracker_id = hass, person_id, tracker_id
        self.entities = (person_id, tracker_id)
        self.path = Path(hass.config.path(".storage", "manual_location_gps.sqlite"))
        self._lock = asyncio.Lock()
        self._healthy = False
        self._closing = False
        self._io_jobs = set()
        self.pending = False
        self.revision = 0
        self.delete_epoch = 0
        self._previews = {}

    @property
    def available(self):
        return self._healthy and not self._closing

    @contextmanager
    def _database(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    async def _io(self, function, *args):
        try:
            future = self.hass.async_add_executor_job(function, *args)
            self._io_jobs.add(future)
            try:
                try:
                    return await asyncio.shield(future)
                except asyncio.CancelledError:
                    # Do not release our lock while an uncancellable SQLite write is running.
                    await asyncio.shield(future)
                    raise
            finally:
                self._io_jobs.discard(future)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            self._healthy = False
            raise GpsHistoryError("history_storage_error") from None

    def _require(self, *, mutation=False):
        if not self.available:
            raise GpsHistoryError("history_unavailable")
        if mutation and self.pending:
            raise GpsHistoryError("history_pending")

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        os.chmod(self.path, 0o600)
        with self._database() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise GpsHistoryError("history_storage_error")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise GpsHistoryError("history_incompatible")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS points (
                    id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                    point TEXT NOT NULL, snapshots TEXT NOT NULL, grant_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL, timestamp REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS points_time ON points(timestamp,id);
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, history_id TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS admissions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL, grant_id TEXT NOT NULL,
                    source_session TEXT NOT NULL, core_boot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS refs (
                    history_id TEXT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
                    entity_id TEXT NOT NULL, timestamp REAL NOT NULL, context_id TEXT NOT NULL,
                    PRIMARY KEY(history_id,entity_id));
                CREATE INDEX IF NOT EXISTS refs_event ON refs(entity_id,timestamp,context_id);
                CREATE TABLE IF NOT EXISTS pending_delete (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS completed_delete (
                    operation_id TEXT PRIMARY KEY, token TEXT NOT NULL, result TEXT NOT NULL);
            """)
            identity = _json(list(self.entities))
            old = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            if old and old[0] != identity:
                raise GpsHistoryError("history_assignment_changed")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('identity',?)", (identity,))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('revision','0')")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('current_generation','0')")
            db.execute("PRAGMA user_version=1")
            return self._revision(db), self._pending(db) is not None

    async def async_initialize(self):
        async with self._lock:
            if self._closing:
                raise GpsHistoryError("history_unavailable")
            self._healthy = False
            self.revision, self.pending = await self._io(self._initialize)
            self._healthy = True

    @staticmethod
    def _revision(db):
        return int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])

    @staticmethod
    def _advance(db):
        db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        return GpsHistory._revision(db)

    @staticmethod
    def _pending(db):
        value = db.execute("SELECT payload FROM pending_delete WHERE singleton=1").fetchone()
        return json.loads(value[0]) if value else None

    def _snapshots(self, snapshots):
        if not isinstance(snapshots, dict) or set(snapshots) != set(self.entities):
            raise GpsHistoryError("history_invalid_input")
        result = {}
        for entity_id, snapshot in snapshots.items():
            if (not isinstance(snapshot, dict) or not _number(snapshot.get("last_updated_ts"))
                    or not isinstance(snapshot.get("context_id"), str) or not snapshot["context_id"]
                    or len(snapshot["context_id"]) > 128):
                raise GpsHistoryError("history_invalid_input")
            if "last_updated" in snapshot:
                _instant(snapshot["last_updated"])
            result[entity_id] = dict(snapshot)
        return result

    @staticmethod
    def _event_refs(snapshots):
        return [{"entity_id": entity_id, "last_updated_ts": value["last_updated_ts"],
                 "context_id": value["context_id"]} for entity_id, value in sorted(snapshots.items())]

    @staticmethod
    def _record(row):
        return {"id": row["id"], "point": json.loads(row["point"]),
                "grant": row["grant_id"], "operation_id": row["operation_id"],
                "snapshots": json.loads(row["snapshots"])}

    def _capture(self, operation_id, grant, point, snapshots, admission=None, promote=True, own_replay=False):
        digest = _digest([grant, point] if own_replay else [grant, point, snapshots])
        event_key = _digest(self._event_refs(snapshots))
        with self._database() as db:
            if self._pending(db):
                raise GpsHistoryError("history_pending")
            old = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if old:
                if old["deleted"]:
                    raise GpsHistoryError("history_operation_retired")
                if old["digest"] != digest:
                    raise GpsHistoryError("history_operation_conflict")
                if admission is not None:
                    bound = db.execute("SELECT * FROM admissions WHERE operation_id=?", (operation_id,)).fetchone()
                    if bound is None or any(bound[key] != admission[key] for key in
                                           ("request_digest", "source_session", "core_boot")):
                        raise GpsHistoryError("history_operation_conflict")
                return old["history_id"], self._revision(db)
            existing = db.execute("SELECT * FROM points WHERE event_key=?", (event_key,)).fetchone()
            if existing:
                if json.loads(existing["point"]) != point:
                    raise GpsHistoryError("history_operation_conflict")
                history_id = existing["id"]
            else:
                history_id = secrets.token_urlsafe(18)
                db.execute("INSERT INTO points VALUES (?,?,?,?,?,?,?)", (
                    history_id, event_key, _json(point), _json(snapshots), grant,
                    operation_id, _instant(point["timestamp"])))
                db.executemany("INSERT INTO refs VALUES (?,?,?,?)", [
                    (history_id, ref["entity_id"], ref["last_updated_ts"], ref["context_id"])
                    for ref in self._event_refs(snapshots)])
            db.execute("INSERT INTO operations(id,digest,history_id) VALUES (?,?,?)",
                       (operation_id, digest, history_id))
            if admission is not None:
                db.execute("INSERT INTO admissions(operation_id,request_digest,grant_id,source_session,core_boot) VALUES (?,?,?,?,?)",
                           (operation_id, admission["request_digest"], grant, admission["source_session"], admission["core_boot"]))
                generation = int(db.execute("SELECT value FROM meta WHERE key='current_generation'").fetchone()[0])
                if promote is True or (type(promote) is int and promote == generation):
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('current_operation',?)", (operation_id,))
            return history_id, self._advance(db)

    async def async_record(self, operation_id, grant, point, snapshots, *, admission=None, promote=True, own_replay=False):
        if (not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 256
                or not isinstance(grant, str) or not 1 <= len(grant) <= 256
                or not isinstance(point, dict) or set(point) != {"latitude", "longitude", "timestamp"}
                or _point(point["latitude"], point["longitude"], point["timestamp"]) is None):
            raise GpsHistoryError("history_invalid_input")
        _instant(point["timestamp"])
        snapshots = self._snapshots(snapshots)
        if own_replay and (grant != "own_handoff" or admission is not None
                           or any(snapshot.get("gps_accuracy") != 0 for snapshot in snapshots.values())):
            raise GpsHistoryError("history_invalid_input")
        if admission is not None and (not isinstance(admission, dict)
                or set(admission) != {"request_digest", "source_session", "core_boot"}
                or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in admission.values())):
            raise GpsHistoryError("history_invalid_input")
        async with self._lock:
            self._require(mutation=True)
            try:
                history_id, self.revision = await self._io(self._capture, operation_id, grant, point, snapshots, admission, promote, own_replay)
            except asyncio.CancelledError:
                # The executor's durable commit finished before _io re-raised.
                marker, self.revision = await self._io(self._read_state)
                self.pending = marker is not None
                raise
            return {"history_id": history_id, "native_timestamp": point["timestamp"]}

    def _get(self, history_id):
        with self._database() as db:
            if history_id in (self._pending(db) or {}).get("gps_ids", []):
                return None
            row = db.execute("SELECT * FROM points WHERE id=?", (history_id,)).fetchone()
            return self._record(row) if row else None

    async def async_get(self, history_id):
        async with self._lock:
            self._require()
            return await self._io(self._get, history_id)

    def _operation(self, binding=None):
        with self._database() as db:
            if binding is None:
                marker = db.execute("SELECT value FROM meta WHERE key='current_operation'").fetchone()
                if not marker or not marker[0]:
                    return None
                operation_id = marker[0]
            else:
                operation_id = binding["operation_id"]
            row = db.execute("SELECT a.*,o.history_id,o.deleted FROM admissions a JOIN operations o ON o.id=a.operation_id WHERE a.operation_id=?",
                             (operation_id,)).fetchone()
            if row is None or row["deleted"]:
                return None
            if binding is not None and any(row[column] != binding[key] for key, column in (
                    ("request_digest", "request_digest"), ("grant", "grant_id"),
                    ("source_session", "source_session"), ("core_boot", "core_boot"))):
                raise GpsHistoryError("history_operation_conflict")
            if row["history_id"] in (self._pending(db) or {}).get("gps_ids", []):
                return None
            point = db.execute("SELECT * FROM points WHERE id=?", (row["history_id"],)).fetchone()
            if point is None:
                return None
            record = self._record(point)
            return {"history_id": row["history_id"], "native_timestamp": record["point"]["timestamp"],
                    "point": record["point"], "snapshots": record["snapshots"],
                    "operation_id": operation_id, "grant": row["grant_id"],
                    "source_session": row["source_session"], "core_boot": row["core_boot"],
                    "request_digest": row["request_digest"], "accepted_seq": row["seq"]}

    async def async_operation(self, binding=None):
        async with self._lock:
            self._require()
            return await self._io(self._operation, binding)

    def _own_current(self):
        with self._database() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('current_operation','')")
            db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='current_generation'")
            return self._advance(db)

    async def async_own_current(self):
        async with self._lock:
            self._require(mutation=True)
            self.revision = await self._io(self._own_current)

    def _generation(self):
        with self._database() as db:
            return int(db.execute("SELECT value FROM meta WHERE key='current_generation'").fetchone()[0])

    async def async_generation(self):
        async with self._lock:
            self._require()
            return await self._io(self._generation)

    @staticmethod
    def _range(start, end):
        lower, upper = _instant(start), _instant(end)
        if not 0 <= upper - lower <= 31 * 86400:
            raise GpsHistoryError("history_invalid_input")
        return lower, upper

    def _points(self, lower, upper, preceding=False, neutral=False):
        with self._database() as db:
            hidden = set((self._pending(db) or {}).get("gps_ids", []))
            columns = "point,id,timestamp" if neutral else "*"
            rows = list(db.execute("SELECT " + columns + " FROM points WHERE timestamp>=? AND timestamp<=? "
                                   "ORDER BY timestamp,id LIMIT ?", (lower, upper, MAX_ROWS + 1)))
            if preceding:
                exclusion = " AND id NOT IN (" + ",".join("?" for _ in hidden) + ")" if hidden else ""
                before = db.execute("SELECT " + columns + " FROM points WHERE timestamp<?" + exclusion
                                    + " ORDER BY timestamp DESC,id DESC LIMIT 1", (lower, *sorted(hidden))).fetchone()
                if before:
                    rows.insert(0, before)
            if len(rows) > MAX_ROWS:
                raise GpsHistoryError("history_too_large")
            return _bounded([json.loads(row["point"]) if neutral else self._record(row)
                             for row in rows if row["id"] not in hidden])

    async def async_points(self, start, end):
        lower, upper = self._range(start, end)
        async with self._lock:
            self._require()
            return await self._io(self._points, lower, upper, True, True)

    @staticmethod
    def _accepted_row(record):
        return {"kind": "accepted", "ref": "accepted:" + record["id"],
                "entity_id": next(key for key in record["snapshots"] if key.startswith("person.")),
                "timestamp": record["point"]["timestamp"], "point": record["point"],
                "origin": "handoff" if record["grant"] == "own_handoff" else "report",
                "fingerprint": _digest([record["id"], record["point"], record["snapshots"]]),
                "event_refs": GpsHistory._event_refs(record["snapshots"])}

    def _page(self, rows, cursor, limit, scope):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise GpsHistoryError("history_invalid_input")
        offset = 0
        if cursor is not None:
            try:
                value = json.loads(cursor)
                if (set(value) != {"revision", "scope", "offset"} or value["revision"] != self.revision
                        or value["scope"] != scope or type(value["offset"]) is not int or value["offset"] < 0):
                    raise ValueError
                offset = value["offset"]
            except (TypeError, ValueError):
                raise GpsHistoryError("history_changed") from None
        rows.sort(key=lambda row: (_instant(row["timestamp"]), row["ref"]), reverse=True)
        # A digest also detects native rows changed/purged between page requests.
        signature = _digest([(row["ref"], row["timestamp"], row.get("fingerprint")) for row in rows])
        if cursor is not None and value["scope"] != scope:
            raise GpsHistoryError("history_changed")
        selected = rows[offset:offset + limit]
        has_more = offset + limit < len(rows)
        next_cursor = _json({"revision": self.revision, "scope": scope, "offset": offset + limit}) if has_more else None
        return _bounded({"rows": selected, "next_cursor": next_cursor, "has_more": has_more,
                         "revision": self.revision, "signature": signature})

    async def async_admin_page(self, cursor=None, limit=100, start=None, end=None):
        async with self._lock:
            self._require()
            lower, upper = self._range(start, end) if start is not None and end is not None else (-1e100, 1e100)
            rows = [self._accepted_row(row) for row in await self._io(self._points, lower, upper)]
            result = self._page(rows, cursor, limit, _digest(["accepted", lower, upper, rows]))
            result["points"] = result["rows"]
            return result

    def _native_instance(self):
        from homeassistant.const import __version__
        from homeassistant.helpers.recorder import get_instance
        if __version__ != "2026.9.4":
            raise GpsHistoryError("history_incompatible")
        try:
            instance = get_instance(self.hass)
            if instance.dialect_name != "sqlite" or instance._get_session is None or not instance.is_alive():
                raise ValueError
        except (KeyError, AttributeError, ValueError):
            raise GpsHistoryError("history_native_unavailable") from None
        return instance

    async def _native_job(self, action, *args):
        from homeassistant.components.recorder.tasks import RecorderTask
        instance = self._native_instance()
        future = self.hass.loop.create_future()
        loop = self.hass.loop

        def finish(value, error):
            if not future.done():
                if error:
                    future.set_exception(error)
                else:
                    future.set_result(value)

        class Job(RecorderTask):
            def run(self, recorder):
                try:
                    result = action(recorder, *args)
                except GpsHistoryError as error:
                    loop.call_soon_threadsafe(finish, None, error)
                except Exception:
                    loop.call_soon_threadsafe(finish, None, GpsHistoryError("history_native_error"))
                else:
                    loop.call_soon_threadsafe(finish, result, None)

        instance.queue_task(Job())
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(future)
            raise

    @staticmethod
    def _native_query(session):
        from sqlalchemy import select
        from homeassistant.components.recorder.db_schema import StateAttributes, States, StatesMeta
        return select(States, StatesMeta.entity_id, StateAttributes.shared_attrs).join(
            StatesMeta, StatesMeta.metadata_id == States.metadata_id).outerjoin(
            StateAttributes, StateAttributes.attributes_id == States.attributes_id)

    @staticmethod
    def _native_row(state, entity_id, shared):
        from homeassistant.util.ulid import bytes_to_ulid_or_none
        attributes = json.loads(shared or state.attributes or "{}")
        timestamp = datetime.fromtimestamp(state.last_updated_ts, UTC).isoformat()
        context = bytes_to_ulid_or_none(state.context_id_bin)
        fingerprint = _digest({column.name: (
            value.hex() if isinstance(value := getattr(state, column.name), bytes) else
            value.isoformat() if isinstance(value, datetime) else value)
            for column in state.__table__.columns
            if column.name not in {"old_state_id", "last_reported_ts"}} | {"shared_attrs": shared, "entity": entity_id})
        return {"kind": "native", "ref": "native:" + str(state.state_id), "id": state.state_id,
                "entity_id": entity_id, "state": state.state, "timestamp": timestamp,
                "point": _point(attributes.get("latitude"), attributes.get("longitude"), timestamp),
                "event_refs": [{"entity_id": entity_id, "last_updated_ts": state.last_updated_ts,
                                "context_id": context}],
                "fingerprint": fingerprint, "attributes_id": state.attributes_id,
                "legacy": state.event_id is not None}

    def _native_rows(self, instance, lower, upper):
        from homeassistant.components.recorder.db_schema import States, StatesMeta
        from homeassistant.helpers.recorder import session_scope
        with session_scope(session=instance.get_session(), read_only=True) as session:
            statement = self._native_query(session).where(
                StatesMeta.entity_id.in_(self.entities), States.last_updated_ts >= lower,
                States.last_updated_ts <= upper).order_by(States.last_updated_ts, States.state_id).limit(MAX_ROWS + 1)
            rows = [self._native_row(*row) for row in session.execute(statement)]
            if len(rows) > MAX_ROWS:
                raise GpsHistoryError("history_too_large")
            return _bounded(rows)

    @staticmethod
    def _binary_ref(ref):
        from homeassistant.components.recorder.models import ulid_to_bytes_or_none
        if "context_id_bin" in ref:
            context = ref["context_id_bin"]
        else:
            try:
                binary = ulid_to_bytes_or_none(ref["context_id"])
                context = binary.hex() if binary else None
            except (TypeError, ValueError):
                raise GpsHistoryError("history_invalid_input") from None
        return ref["entity_id"], ref["last_updated_ts"], context

    async def async_native_page(self, start, end, cursor=None, limit=100):
        lower, upper = self._range(start, end)
        async with self._lock:
            self._require()
            rows = await self._native_job(self._native_rows, lower, upper)
            return self._page(rows, cursor, limit, _digest(["native", lower, upper, rows]))

    async def async_panel_page(self, start, end, cursor=None, limit=100):
        lower, upper = self._range(start, end)
        async with self._lock:
            self._require()
            records = await self._io(self._points, lower, upper)
            accepted = [self._accepted_row(row) for row in records]
            covered = {self._binary_ref(ref) for row in accepted for ref in row["event_refs"]}
            native = await self._native_job(self._native_rows, lower, upper)
            rows = accepted + [row for row in native if self._binary_ref(row["event_refs"][0]) not in covered]
            if len(rows) > MAX_ROWS:
                raise GpsHistoryError("history_too_large")
            return self._page(rows, cursor, limit, _digest(["combined", lower, upper, rows]))

    @staticmethod
    def _snapshot_rows(records, native):
        accepted = [GpsHistory._accepted_row(row) for row in records]
        covered = {GpsHistory._binary_ref(ref) for row in accepted for ref in row["event_refs"]}
        rows = accepted + [row for row in native if GpsHistory._binary_ref(row["event_refs"][0]) not in covered]
        if len(rows) > MAX_ROWS:
            raise GpsHistoryError("history_too_large")
        rows.sort(key=lambda row: (_instant(row["timestamp"]), row["ref"]), reverse=True)
        compact = tuple(_json({key: row[key] for key in (
            "kind", "ref", "entity_id", "timestamp", "point", "origin", "fingerprint"
        ) if key in row}).encode() for row in rows)
        if sum(map(len, compact)) > MAX_BYTES:
            raise GpsHistoryError("history_too_large")
        signature = hashlib.sha256()
        for row in compact:
            signature.update(row)
        return compact, signature.hexdigest()

    async def async_panel_snapshot(self, start, end):
        """Materialize once; the PIN/session owner handles the bounded RAM lifetime."""
        lower, upper = self._range(start, end)
        async with self._lock:
            self._require()
            records = await self._io(self._points, lower, upper)
            native = await self._native_job(self._native_rows, lower, upper)
            rows, signature = await self._io(self._snapshot_rows, records, native)
            return rows, signature, self.revision

    def _selection(self, gps_ids):
        with self._database() as db:
            records = []
            for history_id in gps_ids:
                row = db.execute("SELECT * FROM points WHERE id=?", (history_id,)).fetchone()
                if row is None:
                    raise GpsHistoryError("history_changed")
                records.append(self._record(row))
            return records

    def _native_selection(self, instance, records, native_ids):
        from homeassistant.components.recorder.db_schema import States, StatesMeta
        from homeassistant.components.recorder.models import ulid_to_bytes_or_none
        from homeassistant.helpers.recorder import session_scope
        rows = {}
        missing = 0
        with session_scope(session=instance.get_session(), read_only=True) as session:
            for record in records:
                for ref in self._event_refs(record["snapshots"]):
                    matches = list(session.execute(self._native_query(session).where(
                        StatesMeta.entity_id == ref["entity_id"], States.last_updated_ts == ref["last_updated_ts"],
                        States.context_id_bin == ulid_to_bytes_or_none(ref["context_id"]))))
                    if len(matches) > 1:
                        raise GpsHistoryError("history_ambiguous")
                    if not matches:
                        missing += 1
                    for match in matches:
                        row = self._native_row(*match)
                        # Capture stores only location/state fields, not filtered RAM attributes.
                        snapshot = record["snapshots"][ref["entity_id"]]
                        if snapshot.get("state") is not None and snapshot["state"] != row["state"]:
                            raise GpsHistoryError("history_changed")
                        stored = json.loads(match[2] or match[0].attributes or "{}")
                        if any(key in stored and stored[key] != snapshot.get(key)
                               for key in ("latitude", "longitude", "gps_accuracy")):
                            raise GpsHistoryError("history_changed")
                        rows[row["id"]] = row
            selection_chunk = max(1, instance.max_bind_vars - len(self.entities))
            for offset in range(0, len(native_ids), selection_chunk):
                requested = set(native_ids[offset:offset + selection_chunk])
                matches = list(session.execute(self._native_query(session).where(
                    States.state_id.in_(requested), StatesMeta.entity_id.in_(self.entities))))
                if {match[0].state_id for match in matches} != requested:
                    raise GpsHistoryError("history_changed")
                for match in matches:
                    row = self._native_row(*match)
                    rows[row["id"]] = row
        if any(row["legacy"] for row in rows.values()):
            raise GpsHistoryError("history_legacy_unsupported")
        return list(rows.values()), missing

    def _related(self, refs):
        # Convert each exact native reference back to the index's ULID spelling.
        with self._database() as db:
            candidates = set()
            for entity_id, stamp, context in refs:
                rows = db.execute("SELECT history_id,context_id FROM refs WHERE entity_id=? AND timestamp=?",
                                  (entity_id, stamp))
                for row in rows:
                    if self._binary_ref({"entity_id": entity_id, "last_updated_ts": stamp,
                                         "context_id": row["context_id"]})[2] == context:
                        candidates.add(row["history_id"])
            return candidates

    @staticmethod
    def _ids(values, *, numeric=False):
        if not isinstance(values, list) or len(values) > MAX_SELECTION:
            raise GpsHistoryError("history_invalid_input")
        if any((type(value) is not int or value < 1) if numeric else
               (not isinstance(value, str) or not 1 <= len(value) <= 128) for value in values):
            raise GpsHistoryError("history_invalid_input")
        if len(set(values)) != len(values):
            raise GpsHistoryError("history_invalid_input")
        return list(values)

    async def async_preview(self, gps_ids, native_ids, current_snapshots, expected_fingerprints=None):
        gps_ids, native_ids = self._ids(gps_ids), self._ids(native_ids, numeric=True)
        if not gps_ids and not native_ids:
            raise GpsHistoryError("history_invalid_input")
        current = self._event_refs(self._snapshots(current_snapshots))
        async with self._lock:
            self._require(mutation=True)
            records = await self._io(self._selection, gps_ids)
            rows, missing = await self._native_job(self._native_selection, records, native_ids)
            if expected_fingerprints:
                current_rows = {row["ref"]: row for row in rows}
                current_rows.update((row["ref"], row) for row in map(self._accepted_row, records))
                if any(ref not in current_rows or current_rows[ref]["fingerprint"] != fingerprint
                       for ref, fingerprint in expected_fingerprints.items()):
                    raise GpsHistoryError("history_changed")
            return await self._make_preview(records, rows, missing, current)

    async def _make_preview(self, records, rows, missing, current, *, sampled=False,
                            allow_current=False, range_bounds=None):
        gps_ids = [record["id"] for record in records]
        refs = {self._binary_ref(ref) for record in records for ref in self._event_refs(record["snapshots"])}
        refs.update(self._binary_ref(row["event_refs"][0]) for row in rows)
        if not allow_current and refs.intersection(self._binary_ref(ref) for ref in current):
            raise GpsHistoryError("history_current_protected")
        if await self._io(self._related, refs) - set(gps_ids):
            raise GpsHistoryError("history_range_crosses_group" if range_bounds else "history_incomplete_group")
        if range_bounds and any(not range_bounds[0] <= ref["last_updated_ts"] <= range_bounds[1]
                                for record in records for ref in self._event_refs(record["snapshots"])):
            raise GpsHistoryError("history_range_crosses_group")
        if any(row["legacy"] for row in rows):
            raise GpsHistoryError("history_legacy_unsupported")
        token = secrets.token_urlsafe(24)
        expires = time.time() + PREVIEW_SECONDS
        visible_rows = [self._accepted_row(row) for row in records] + rows
        visible_rows.sort(key=lambda row: (_instant(row["timestamp"]), row["ref"]), reverse=True)
        preview = {"token": token, "revision": self.revision, "gps_ids": gps_ids,
                   "native_ids": sorted(row["id"] for row in rows),
                   "counts": {"accepted": len(records), "native": len(rows), "missing_native": missing,
                              "persons": sum(row["entity_id"] == self.person_id for row in rows),
                              "trackers": sum(row["entity_id"] == self.tracker_id for row in rows)},
                   "rows": visible_rows[:100],
                   "truncated": len(visible_rows) > 100,
                   "allow_current": allow_current,
                   "expires_at": datetime.fromtimestamp(expires, UTC).isoformat()}
        _bounded({"gps_ids": gps_ids, "native": rows})
        self._previews = {key: value for key, value in self._previews.items() if value["expires"] > time.time()}
        if len(self._previews) >= 32:
            self._previews.pop(next(iter(self._previews)))
        self._previews[token] = {"preview": preview, "native": rows, "current": current, "expires": expires}
        if sampled:
            # Full exact selections stay on the server, never a silent 100-row deletion.
            return _bounded(preview | {"gps_ids": [], "native_ids": []})
        return _bounded(preview)

    async def async_range_preview(self, start, end, current_snapshots, *, allow_empty=False, allow_current=False):
        if type(allow_empty) is not bool or type(allow_current) is not bool:
            raise GpsHistoryError("history_invalid_input")
        lower, upper = self._range(start, end)
        current = self._event_refs(self._snapshots(current_snapshots))
        async with self._lock:
            self._require(mutation=True)
            records = await self._io(self._points, lower, upper)
            rows = await self._native_job(self._native_rows, lower, upper)
            if not records and not rows and not allow_empty:
                raise GpsHistoryError("history_empty_selection")
            if len(records) + len(rows) > MAX_ROWS:
                raise GpsHistoryError("history_too_large")
            return await self._make_preview(records, rows, 0, current, sampled=True,
                                            allow_current=allow_current, range_bounds=(lower, upper))

    def _begin_delete(self, operation_id, token, value, own_plan):
        with self._database() as db:
            completed = db.execute("SELECT * FROM completed_delete WHERE operation_id=?", (operation_id,)).fetchone()
            if completed:
                if completed["token"] != token:
                    raise GpsHistoryError("history_operation_conflict")
                return json.loads(completed["result"]), None, self._revision(db)
            if self._pending(db):
                raise GpsHistoryError("history_pending")
            if self._revision(db) != value["preview"]["revision"]:
                raise GpsHistoryError("history_changed")
            marker = {"operation_id": operation_id, "token": token,
                      "gps_ids": value["preview"]["gps_ids"], "counts": value["preview"]["counts"],
                      "allow_current": value["preview"]["allow_current"],
                      "own_plan": own_plan,
                      "native": [{key: row[key] for key in ("id", "entity_id", "fingerprint", "event_refs")}
                                 for row in value["native"]]}
            db.execute("INSERT INTO pending_delete VALUES (1,?)", (_json(_bounded(marker)),))
            revision = self._advance(db)
            return None, marker, revision

    def _delete_native(self, instance, marker):
        from sqlalchemy import select
        from homeassistant.components.recorder.db_schema import States
        from homeassistant.components.recorder.purge import _purge_state_ids, _purge_unused_attributes_ids
        from homeassistant.helpers.recorder import session_scope
        rows, attributes = set(), set()
        expected_rows = {row["id"]: row for row in marker["native"]}
        ids = sorted(expected_rows)
        with session_scope(session=instance.get_session()) as session:
            for offset in range(0, len(ids), instance.max_bind_vars):
                matches = session.execute(self._native_query(session).where(
                    States.state_id.in_(ids[offset:offset + instance.max_bind_vars])))
                for match in matches:
                    row = self._native_row(*match)
                    expected = expected_rows[row["id"]]
                    if (row["entity_id"] not in self.entities or row["entity_id"] != expected["entity_id"]
                            or row["fingerprint"] != expected["fingerprint"] or row["legacy"]):
                        raise GpsHistoryError("history_changed")
                    rows.add(row["id"])
                    if row["attributes_id"] is not None:
                        attributes.add(row["attributes_id"])
            # disconnect_states_rows also binds the NULL value for old_state_id.
            delete_chunk = max(1, instance.max_bind_vars - 1)
            sorted_rows = sorted(rows)
            for offset in range(0, len(sorted_rows), delete_chunk):
                _purge_state_ids(instance, session, set(sorted_rows[offset:offset + delete_chunk]))
            _purge_unused_attributes_ids(instance, session, attributes)
        with session_scope(session=instance.get_session(), read_only=True) as session:
            for offset in range(0, len(ids), instance.max_bind_vars):
                if session.execute(select(States.state_id).where(
                        States.state_id.in_(ids[offset:offset + instance.max_bind_vars]))).first():
                    raise GpsHistoryError("history_native_error")
            instance.states_manager.load_from_db(session)

    def _finish_delete(self, marker):
        with self._database() as db:
            if self._pending(db) != marker:
                raise GpsHistoryError("history_changed")
            for history_id in marker["gps_ids"]:
                db.execute("UPDATE operations SET deleted=1,digest='' WHERE history_id=?", (history_id,))
                db.execute("DELETE FROM points WHERE id=?", (history_id,))
            db.execute("DELETE FROM pending_delete WHERE singleton=1")
            revision = self._advance(db)
            result = {"counts": marker["counts"], "revision": revision, "completed": True}
            db.execute("INSERT INTO completed_delete VALUES (?,?,?)",
                       (marker["operation_id"], marker["token"], _json(result)))
            return result

    def _completed(self, operation_id, token):
        with self._database() as db:
            row = db.execute("SELECT token,result FROM completed_delete WHERE operation_id=?", (operation_id,)).fetchone()
            if row:
                if row["token"] != token:
                    raise GpsHistoryError("history_operation_conflict")
                return json.loads(row["result"])
            return None

    async def async_execute(self, preview_token, operation_id, *, own_plan=None, own_commit=None):
        self.delete_epoch += 1
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 256:
            raise GpsHistoryError("history_invalid_input")
        if own_plan is not None and (not isinstance(own_plan, dict) or own_commit is None):
            raise GpsHistoryError("history_invalid_input")
        async with self._lock:
            self._require()
            if completed := await self._io(self._completed, operation_id, preview_token):
                return completed
            value = self._previews.get(preview_token)
            if value is None or value["expires"] <= time.time():
                raise GpsHistoryError("history_preview_expired")
            for ref in value["current"]:
                state = self.hass.states.get(ref["entity_id"])
                if (state is None or state.last_updated_timestamp != ref["last_updated_ts"]
                        or state.context.id != ref["context_id"]):
                    raise GpsHistoryError("history_changed")
            self.pending = True
            try:
                _, marker, self.revision = await self._io(self._begin_delete, operation_id, preview_token, value, own_plan)
            except GpsHistoryError:
                if self.available:
                    marker, self.revision = await self._io(self._read_state)
                    self.pending = marker is not None
                raise
            if marker["native"]:
                await self._native_job(self._delete_native, marker)
            if marker.get("own_plan") is not None:
                await own_commit(marker["own_plan"])
            result = await self._io(self._finish_delete, marker)
            self.pending = False
            self.revision = result["revision"]
            self._previews.clear()
            return result

    def _read_state(self):
        with self._database() as db:
            # One SQLite read snapshot: cancellation may have committed cleanup
            # without delivering its new revision back to the event loop.
            row = db.execute("SELECT (SELECT payload FROM pending_delete WHERE singleton=1),"
                             "(SELECT value FROM meta WHERE key='revision')").fetchone()
            return json.loads(row[0]) if row[0] else None, int(row[1])

    async def async_recover(self, current_snapshots, *, own_commit=None):
        self.delete_epoch += 1
        current = {self._binary_ref(ref) for ref in self._event_refs(self._snapshots(current_snapshots))}
        async with self._lock:
            if self._closing:
                raise GpsHistoryError("history_unavailable")
            marker, self.revision = await self._io(self._read_state)
            if marker is None:
                self.pending = False
                return True
            self.pending = True
            if not marker.get("allow_current", False) and any(
                    self._binary_ref(ref) in current for row in marker["native"] for ref in row["event_refs"]):
                raise GpsHistoryError("history_current_protected")
            if marker.get("own_plan") is not None and own_commit is None:
                raise GpsHistoryError("history_own_commit_required")
            if marker["native"]:
                await self._native_job(self._delete_native, marker)
            if marker.get("own_plan") is not None:
                await own_commit(marker["own_plan"])
            result = await self._io(self._finish_delete, marker)
            self.revision, self.pending, self._healthy = result["revision"], False, True
            self._previews.clear()
            return True

    def _backup(self, target):
        target = Path(target)
        if not target.is_absolute() or target.resolve() == self.path.resolve():
            raise GpsHistoryError("history_invalid_input")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        os.close(descriptor)
        try:
            with self._database() as source:
                destination = sqlite3.connect(temporary)
                try:
                    source.backup(destination, pages=256, sleep=0.01)
                    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise GpsHistoryError("history_storage_error")
                finally:
                    destination.close()
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    async def async_backup(self, target):
        # HA may already hold Recorder's backup lock. A deletion can hold our
        # lock while awaiting Recorder, so backup must use SQLite's own snapshot.
        self._require()
        await self._io(self._backup, target)

    async def async_close(self):
        """Reject new work, then drain mutations and independent online backups."""
        self._closing = True
        self._healthy = False
        async with self._lock:
            while self._io_jobs:
                await asyncio.gather(*(asyncio.shield(job) for job in tuple(self._io_jobs)),
                                     return_exceptions=True)
            self._healthy = False
            self._previews.clear()

    @staticmethod
    async def async_remove_files(hass):
        """Remove only this integration's index and local backup after unloading."""
        def remove():
            directory = Path(hass.config.path(".storage"))
            if not directory.exists():
                return
            for filename in ("manual_location_gps.sqlite", "manual_location_gps.backup.sqlite"):
                for suffix in ("", "-journal", "-wal", "-shm"):
                    (directory / (filename + suffix)).unlink(missing_ok=True)
            # mkstemp's exact eight-character namespace; never traverse folders
            # or match archived backups and another integration's files.
            temporary = re.compile(r"manual_location_gps\.backup\.sqlite\.[a-z0-9_]{8}\.tmp(?:-journal|-wal|-shm)?")
            with os.scandir(directory) as entries:
                for entry in entries:
                    if temporary.fullmatch(entry.name) and not entry.is_dir(follow_symlinks=False):
                        Path(entry.path).unlink()
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        try:
            await hass.async_add_executor_job(remove)
        except OSError:
            raise GpsHistoryError("history_storage_error") from None

    async def async_remove(self):
        await self.async_close()
        await self.async_remove_files(self.hass)
