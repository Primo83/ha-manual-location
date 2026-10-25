"""Durable, local GPS permissions deliberately excluded from HA backups."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

MAX_SCOPES = 101
MAX_AUTHORITY_BYTES = 131072
_SCOPE_BASE = {"id", "person_id", "tracker_id", "phone_entry_id"}


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def scopes(value, person_id):
    if not isinstance(value, list) or len(value) > MAX_SCOPES:
        raise ValueError("authorization scopes")
    result, seen = [], set()
    for item in value:
        if (not isinstance(item, dict) or set(item) not in (
                _SCOPE_BASE, _SCOPE_BASE | {"run_id", "scope_digest"})
                or not identifier(item.get("id")) or len(item["id"]) < 16
                or item["id"] in seen or item.get("person_id") != person_id
                or not isinstance(item.get("tracker_id"), str)
                or not re.fullmatch(r"device_tracker\.[a-z0-9_]+", item["tracker_id"])
                or not identifier(item.get("phone_entry_id"))):
            raise ValueError("authorization scope")
        if "run_id" in item and (not identifier(item["run_id"])
                or not isinstance(item["scope_digest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", item["scope_digest"]) is None):
            raise ValueError("authorization route")
        seen.add(item["id"])
        result.append(dict(item))
    return sorted(result, key=lambda item: item["id"])


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class Authorizations:
    """One fsynced record, with a durable fail-closed interrupted-write latch."""

    def __init__(self, path, person_id, webhook_id):
        self.path = Path(path)
        self.marker = self.path.with_name(self.path.name + ".blocked")
        self.temporary = self.path.with_name(self.path.name + ".tmp")
        self.person_id, self.webhook_id = person_id, webhook_id
        self.error = None
        self.data = self._empty()
        try:
            if self.marker.exists():
                raise ValueError("authorization write interrupted")
            raw = self.path.read_bytes()
            if len(raw) > MAX_AUTHORITY_BYTES:
                raise ValueError("authorization record size")
            value = json.loads(raw)
            if (not isinstance(value, dict) or set(value) != {
                    "version", "person_id", "webhook_id", "generation", "scopes",
                    "active_intent_id", "last_operation"}
                    or value["version"] != 1 or value["person_id"] != person_id
                    or value["webhook_id"] != webhook_id or not identifier(value["generation"])):
                raise ValueError("authorization identity")
            value["scopes"] = scopes(value["scopes"], person_id)
            active = value["active_intent_id"]
            if active is not None and active not in {item["id"] for item in value["scopes"]}:
                raise ValueError("authorization active scope")
            operation = value["last_operation"]
            if operation is not None and (
                    not isinstance(operation, dict) or set(operation) != {"key", "digest"}
                    or not identifier(operation["key"]) or not isinstance(operation["digest"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", operation["digest"]) is None):
                raise ValueError("authorization receipt")
            self.data = value
        except FileNotFoundError:
            self.error = "missing"
        except (OSError, ValueError, TypeError, KeyError):
            self.error = "corrupt"

    def _empty(self):
        return {"version": 1, "person_id": self.person_id, "webhook_id": self.webhook_id,
                "generation": uuid.uuid4().hex, "scopes": [], "active_intent_id": None,
                "last_operation": None}

    @property
    def generation(self):
        return self.data["generation"]

    @property
    def active(self):
        return self.data["active_intent_id"]

    def scope(self, intent_id):
        return next((deepcopy(item) for item in self.data["scopes"] if item["id"] == intent_id), None)

    def repeated(self, body):
        previous = self.data["last_operation"]
        return bool(previous and previous["key"] == body["idempotency_key"]
                    and previous["digest"] == digest(body))

    def _sync_directory(self):
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def write(self, desired, active=None, *, operation=None):
        desired = scopes(desired, self.person_id)
        if active is not None and active not in {item["id"] for item in desired}:
            raise ValueError("authorization active scope")
        value = {**self.data, "generation": uuid.uuid4().hex, "scopes": desired,
                 "active_intent_id": active, "last_operation": (
                     {"key": operation["idempotency_key"], "digest": digest(operation)}
                     if operation is not None else None)}
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_AUTHORITY_BYTES:
            raise ValueError("authorization record size")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # The directory entry must be durable before replacing the old permission.
            descriptor = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"blocked\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._sync_directory()
            descriptor = os.open(self.temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(self.temporary, self.path)
            self._sync_directory()
            self.marker.unlink()
            self._sync_directory()
        except BaseException:
            self.data, self.error = self._empty(), "write_failed"
            # Also cover errors after unlink: retain an observable latch when possible.
            try:
                self.marker.touch(mode=0o600, exist_ok=True)
                self._sync_directory()
            except OSError:
                pass
            raise
        self.data, self.error = value, None

    def revoke_active(self):
        active = self.active
        if active is not None:
            self.write([item for item in self.data["scopes"] if item["id"] != active])
