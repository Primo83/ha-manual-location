"""Companion ingress filter with an isolated laboratory default.

Gateway(...).app is an aiohttp Application. Run it with
``runner = web.AppRunner(gateway.app, access_log=None)`` and
``web.TCPSite(runner, '127.0.0.1', port)``. The backend URL must use literal
127.0.0.1. The advertised public URL may additionally use literal 192.0.2.1
or 2001:db8:1::1 for the isolated documentation-subnet namespace harness.
The explicit production_mode accepts an advertised HTTPS DNS origin while the
backend remains literal loopback. Staging TLS uses a verified public hostname;
there is no certificate-verification bypass. Production preserves Core's usual
API authorization and JSON responses, filtering the selected mobile webhooks.
The native harness supplies async checkpoint_native(point, *, phase) and
verify_native(point)->bool; point has person_id, latitude, longitude,
gps_accuracy. The checkpoint callback awaits phase('checkpoint_inflight')
before its mutating RPC and phase('checkpoint_closed') after its full 200
response, before disk verification/fsync. ready_native()->bool is optional
(synthetic default True); it gates attempts but cannot resolve uncertainty.

Control uses Authorization: Bearer <control_token>, independently of HA auth.
POST /control/custom: {idempotency_key, point:{latitude,longitude,gps_accuracy}}
(point is optional for closing only). POST /control/normal: {idempotency_key}.
GET /control/status and /health require the same control token. Each instance
protects one person and one existing registration. New registration is denied.
The protected registration accepts clear JSON only; encrypted envelopes fail
closed. Production additionally requires a runtime registration guard and
reconstructs registration metadata without arbitrary app_data attributes.
The latest custom operation is idempotent across restart. Startup and failed
replay stay custom; native-harness may call await replay_last_custom() after
HA restarts. A replay is always origin=custom and never a phone refresh.
Only own custom points are checkpointed. There are no body/token log calls.
Before a native mutation, a durable upstream_uncertain marker is written.
Only its correlated successful response and required verification can clear
it. Timeout/cancellation/error keeps the marker across gateway restart and
denies custom/normal/replay. Durable own intents in location_closed or
checkpoint_closed can roll forward automatically to that same own target.
Inflight own/phone/opaque uncertainty never auto-clears; there is no clear
endpoint. A GET cannot prove that a disconnected backend handler has stopped.
Missing checkpoints are uncertain by default. allow_fresh_setup=True is only
for the first start of a newly created isolated registration; it is not a
recovery/clear control and must never be reused on gateway restart.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
import hashlib
import hmac
import json
import math
import os
import re
import ssl
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web

_UNCHANGED = object()
ACTIVITY_SENSORS = {
    "is_interactive": ("binary_sensor", "Interactive"),
    "keyguard_locked": ("binary_sensor", "Keyguard locked"),
    "detected_activity": ("sensor", "Detected activity"),
}
ACTIVITY_VALUES = frozenset({"walking", "on_foot", "running", "still", "tilting", "in_vehicle", "on_bicycle"})


async def _synthetic_ready():
    return True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json(data):
    return json.loads(data, object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


def _loopback_url(value, *, advertised=False):
    parsed = urlsplit(value)
    allowed_hosts = {"127.0.0.1", "192.0.2.1", "2001:db8:1::1"} if advertised else {"127.0.0.1"}
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed_hosts
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("isolated lab origin required")
    parsed.port  # Validate numeric port/range without resolving any host.
    return value.rstrip("/")


def _production_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}
            or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                                r"[a-z][a-z0-9-]{0,61}[a-z0-9]", parsed.hostname)
            or len(parsed.hostname) > 253):
        raise ValueError("production HTTPS origin required")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("production origin port")
    return value.rstrip("/")


def _point(value, person_id):
    if not isinstance(value, dict) or set(value) - {"latitude", "longitude", "gps_accuracy"}:
        raise ValueError("point schema")
    result = {"person_id": person_id}
    for key in ("latitude", "longitude", "gps_accuracy"):
        number = value.get(key, 0 if key == "gps_accuracy" else None)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError("point number")
        result[key] = number
    if not (-90 <= result["latitude"] <= 90 and -180 <= result["longitude"] <= 180
            and result["gps_accuracy"] >= 0):
        raise ValueError("point range")
    return result


def _payload(point):
    return {"type": "update_location", "data": {
        "gps": [point["latitude"], point["longitude"]],
        "gps_accuracy": point["gps_accuracy"],
    }}


def _custom_point_digest(point, person_id):
    """Fingerprint Own coordinates using the same canonical payload as the HA bridge."""
    if point is None:
        return None
    payload = _point({key: point[key] for key in ("latitude", "longitude")}, person_id)
    for key in ("latitude", "longitude"):
        payload[key] = float(payload[key]) or 0.0
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


class Gateway:
    def __init__(self, backend_url, public_url, webhook_id, control_token,
                 checkpoint_path, checkpoint_native, verify_native, *,
                 person_id="lab-person", clock=time.monotonic,
                 warmup_seconds=300, max_body_bytes=65536, upstream_timeout_seconds=5,
                 allow_fresh_setup=False, ready_native=None, recovery_retry_seconds=1,
                 source_session_check=None, source_control_check=None, passthrough_webhooks=(),
                 production_mode=False, max_ws_message_bytes=None, backend_ssl_server_hostname=None,
                 denied_webhooks=(), allowed_push_url=None, registration_guard=None, backend_ssl_context=None,
                 phone_snapshot=None, record_phone=None, wall_clock=time.time):
        if type(production_mode) is not bool:
            raise ValueError("production mode")
        self.production_mode = production_mode
        self.backend_url = _loopback_url(backend_url)
        self.public_url = _production_origin(public_url) if production_mode else _loopback_url(public_url, advertised=True)
        if backend_ssl_server_hostname is not None and (
                not production_mode or urlsplit(self.backend_url).scheme != "https"
                or backend_ssl_server_hostname != urlsplit(self.public_url).hostname):
            raise ValueError("backend TLS server hostname")
        self.backend_request_options = {"server_hostname": backend_ssl_server_hostname} if backend_ssl_server_hostname else {}
        if backend_ssl_context is not None:
            if (not production_mode or urlsplit(self.backend_url).scheme != "https"
                    or backend_ssl_server_hostname is None or not isinstance(backend_ssl_context, ssl.SSLContext)
                    or backend_ssl_context.verify_mode != ssl.CERT_REQUIRED or not backend_ssl_context.check_hostname):
                raise ValueError("verified backend TLS context required")
            self.backend_request_options["ssl"] = backend_ssl_context
        if production_mode and registration_guard is None:
            raise ValueError("production registration guard required")
        if (phone_snapshot is None) != (record_phone is None) or (production_mode and phone_snapshot is None):
            raise ValueError("phone history callbacks required")
        if phone_snapshot is not None and (not callable(phone_snapshot) or not callable(record_phone)):
            raise ValueError("phone history callbacks required")
        self.phone_snapshot, self.record_phone, self.wall_clock = phone_snapshot, record_phone, wall_clock
        self.normal_grant = self.proven_phone = None
        self.registration_guard = registration_guard
        if allowed_push_url is not None:
            push = urlsplit(allowed_push_url)
            if (push.scheme != "https" or not push.hostname or push.username is not None
                    or push.password is not None or push.fragment):
                raise ValueError("push provider HTTPS URL")
        self.allowed_push_url = allowed_push_url
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", webhook_id):
            raise ValueError("webhook identifier")
        if (not control_token or warmup_seconds < 300 or upstream_timeout_seconds <= 0
                or recovery_retry_seconds <= 0):
            raise ValueError("control token and >=300s warmup required")
        self.webhook_id, self.control_token = webhook_id, control_token
        self.person_id, self.clock, self.warmup_seconds = person_id, clock, warmup_seconds
        self.source_session_check = source_session_check
        self.source_control_check = source_control_check
        self.source_session = None
        if isinstance(passthrough_webhooks, (str, bytes)) or any(
                not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item)
                or item == webhook_id for item in passthrough_webhooks):
            raise ValueError("passthrough registrations")
        self.passthrough_webhooks = frozenset(passthrough_webhooks)
        if isinstance(denied_webhooks, (str, bytes)) or any(
                not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item)
                or item in self.passthrough_webhooks | {webhook_id} for item in denied_webhooks):
            raise ValueError("denied registrations")
        self.denied_webhooks = frozenset(denied_webhooks)
        self.max_body_bytes = max_body_bytes
        self.max_ws_message_bytes = max_ws_message_bytes if max_ws_message_bytes is not None else (
            8 * 1024 * 1024 if production_mode else max_body_bytes)
        if (type(self.max_ws_message_bytes) is not int or self.max_ws_message_bytes < max_body_bytes):
            raise ValueError("WebSocket message limit")
        self.upstream_timeout_seconds = upstream_timeout_seconds
        self.allow_fresh_setup = allow_fresh_setup
        self.checkpoint_path = Path(checkpoint_path)
        self.checkpoint_native, self.verify_native = checkpoint_native, verify_native
        self.ready_native = ready_native if ready_native is not None else _synthetic_ready
        self.recovery_retry_seconds = recovery_retry_seconds
        self.recovery_task = None
        self.recovery_active = False
        self.lock = asyncio.Lock()  # One isolated instance/lock per protected person.
        self.mode, self.normal_after = "custom", None
        self.last_custom = self.last_custom_receipt = self.last_phone_receipt = None
        self.last_operation = None
        self.sequence = 0
        self.normal_operations = {}
        self.replay_required = False
        self.upstream_uncertain = False
        self.uncertainty_origin = None
        self.pending_custom = None
        self.scheduled_return = self.return_receipt = None
        self._return_deadline = self._return_clock = None
        self._return_dirty = False
        self._load_checkpoint()
        self.app = web.Application(client_max_size=self.max_ws_message_bytes if production_mode else max_body_bytes)
        self.app.router.add_route("*", "/{path:.*}", self._handle)
        self.app.on_startup.append(self._startup)
        self.app.on_cleanup.append(self._cleanup)

    def _load_checkpoint(self):
        try:
            state = _json(self.checkpoint_path.read_bytes())
            if not isinstance(state, dict) or state.get("person_id") != self.person_id or state.get("version") not in {1, 2, 3}:
                raise ValueError("checkpoint identity")
            if state["version"] == 3:
                plan = state["scheduled_return"]
                if (not isinstance(plan, dict) or set(plan) != {"id", "at", "state"}
                        or not self._return_id(plan["id"]) or plan["state"] not in {"idle", "armed", "due", "applied"}
                        or (plan["at"] is not None if plan["state"] == "idle" else
                            type(plan["at"]) is not int or not 0 <= plan["at"] <= 253402300799)):
                    raise ValueError("scheduled return checkpoint")
                receipt = state.get("return_receipt")
                if receipt is not None and (
                        not isinstance(receipt, dict) or set(receipt) != {"key", "digest", "from_id", "to_id"}
                        or not self._return_id(receipt["key"]) or not self._return_id(receipt["to_id"])
                        or (receipt["from_id"] is not None and not self._return_id(receipt["from_id"]))
                        or not isinstance(receipt["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["digest"])
                        or receipt["to_id"] != plan["id"]):
                    raise ValueError("scheduled return receipt")
                self.scheduled_return, self.return_receipt = plan, receipt
                self._set_return_clock()
            if not isinstance(state.get("upstream_uncertain", False), bool):
                raise ValueError("checkpoint uncertainty")
            self.upstream_uncertain = state.get("upstream_uncertain", False)
            self.uncertainty_origin = "unknown" if self.upstream_uncertain else None
            if state["version"] in {2, 3} and self.upstream_uncertain:
                origin = state.get("uncertainty_origin")
                if origin not in {"custom", "phone", "unknown"}:
                    raise ValueError("uncertainty origin")
                self.uncertainty_origin = origin
                if origin == "custom":
                    intent = state.get("pending_custom")
                    if not isinstance(intent, dict) or set(intent) != {"point", "operation", "phase"}:
                        raise ValueError("own intent")
                    if not isinstance(intent["point"], dict) or not isinstance(intent["operation"], dict):
                        raise ValueError("own intent objects")
                    point = _point({k: v for k, v in intent["point"].items() if k != "person_id"}, self.person_id)
                    operation = self._operation(intent["operation"]["key"], point)
                    if operation != intent["operation"] or intent["phase"] not in {
                            "own_location_inflight", "location_closed", "checkpoint_inflight", "checkpoint_closed"}:
                        raise ValueError("own intent identity/phase")
                    self.pending_custom = {"point": point, "operation": operation, "phase": intent["phase"]}
            saved = state.get("last_custom")
            if saved is not None:
                self.last_custom = _point({k: v for k, v in saved.items() if k != "person_id"}, self.person_id)
                self.last_operation = state.get("operation")
                self.last_custom_receipt = state.get("custom_receipt")
                self.sequence = int(state.get("sequence", 0))
                self.replay_required = True
        except FileNotFoundError:
            self.upstream_uncertain = not self.allow_fresh_setup
            self.uncertainty_origin = "unknown" if self.upstream_uncertain else None
        except (OSError, ValueError, TypeError, KeyError):
            # Existing unreadable/corrupt state cannot disprove an in-flight mutation.
            self.upstream_uncertain = True
            self.uncertainty_origin, self.pending_custom = "unknown", None
            self.last_custom = self.last_operation = self.last_custom_receipt = None

    def _save(self, *, last_custom, operation, receipt, pending=_UNCHANGED, upstream_uncertain=None):
        uncertain = self.upstream_uncertain if upstream_uncertain is None else upstream_uncertain
        state = {"version": 3 if self.scheduled_return is not None else 2, "person_id": self.person_id, "mode": "custom",
                 "last_custom": last_custom, "operation": operation,
                 "custom_receipt": receipt,
                 "pending_custom": self.pending_custom if pending is _UNCHANGED else pending,
                 "sequence": self.sequence,
                 "upstream_uncertain": uncertain,
                 "uncertainty_origin": self.uncertainty_origin if uncertain else None}
        if self.scheduled_return is not None:
            state.update(scheduled_return=self.scheduled_return, return_receipt=self.return_receipt)
        data = json.dumps(state, allow_nan=False, separators=(",", ":")).encode()
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint_path.with_name(self.checkpoint_path.name + ".tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.checkpoint_path)
            descriptor = os.open(self.checkpoint_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        self._return_dirty = False

    @staticmethod
    def _return_id(value):
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None

    def _set_return_clock(self):
        self._return_deadline = self._return_clock = None
        if self.scheduled_return is not None and self.scheduled_return["state"] == "armed":
            wall, mono = self.wall_clock(), self.clock()
            if (type(wall) not in {int, float} or type(mono) not in {int, float}
                    or not math.isfinite(wall) or not math.isfinite(mono) or wall < 0 or mono < 0):
                raise ValueError("scheduled return clock")
            self._return_clock = mono
            self._return_deadline = mono + max(0, self.scheduled_return["at"] - wall)

    def _expire_return(self):
        """Revoke admission without awaiting the writer or changing its uncertainty."""
        plan = self.scheduled_return
        if plan is None or plan["state"] == "idle":
            return False
        if plan["state"] == "armed":
            wall, mono = self.wall_clock(), self.clock()
            invalid = (type(wall) not in {int, float} or type(mono) not in {int, float}
                       or not math.isfinite(wall) or not math.isfinite(mono) or wall < 0
                       or self._return_clock is None or mono < self._return_clock)
            if not invalid:
                self._return_clock = mono
            if invalid or wall >= plan["at"] or mono >= self._return_deadline:
                self.scheduled_return = {**plan, "id": uuid.uuid4().hex, "state": "due"}
                self.return_receipt = None
                self._return_dirty = True
                self.replay_required = True
                self._return_deadline = self._return_clock = None
        if self.scheduled_return["state"] in {"due", "applied"}:
            self._fail_native()
            return True
        return False

    def _save_return(self):
        self._save(last_custom=self.last_custom, operation=self.last_operation,
                   receipt=self.last_custom_receipt)

    def _apply_return(self):
        self._expire_return()
        if (self.scheduled_return is not None and self.scheduled_return["state"] == "due"
                and not self.upstream_uncertain and not self.replay_required):
            if self._return_dirty:
                self._save_return()
            self.scheduled_return = {**self.scheduled_return, "state": "applied"}
            self._return_dirty = True
            self._save_return()

    async def _recover_tick(self):
        self._expire_return()
        if self._return_dirty:
            async with self.lock:
                self._save_return()  # Preserve any phone/own marker already owned by the writer.
        if self._recoverable():
            await self.recover_pending_custom()
        elif self.replay_required and not self.upstream_uncertain:
            await self.replay_last_custom()

    async def _startup(self, _app):
        self.session = ClientSession(timeout=ClientTimeout(total=self.upstream_timeout_seconds), trust_env=False,
                                     cookie_jar=DummyCookieJar() if self.production_mode else None)
        try:
            await self._recover_tick()
        except (OSError, ValueError):
            self._fail_native()
        self.recovery_task = asyncio.create_task(self._recovery_loop())

    async def _cleanup(self, _app):
        if self.recovery_task is not None:
            self.recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.recovery_task
        await self.session.close()

    def _recoverable(self):
        return bool(self.upstream_uncertain and self.uncertainty_origin == "custom"
                    and self.pending_custom and self.pending_custom["phase"] in {"location_closed", "checkpoint_closed"})

    async def _recovery_loop(self):
        while True:
            await asyncio.sleep(self.recovery_retry_seconds)
            try:
                await self._recover_tick()
            except (OSError, ValueError):
                self._fail_native()

    def _mode(self):
        self._expire_return()
        if self.upstream_uncertain:
            return "custom"
        if self.mode == "warmup" and self.normal_after is not None and self.clock() >= self.normal_after:
            self.mode = "normal"
        return self.mode

    def _authorized(self, request):
        expected = "Bearer " + self.control_token
        return hmac.compare_digest(request.headers.get("Authorization", ""), expected)

    async def _ensure_source_session(self):
        if self.mode in {"warmup", "normal"} and not await self._ensure_registration():
            return False
        if self.source_session_check is None:
            return True  # Standalone lab compatibility; integrated runtime supplies a check.
        if self.mode not in {"warmup", "normal"}:
            return False
        try:
            valid = bool(self.source_session and await self.source_session_check(self.source_session))
        except Exception:
            valid = False
        if not valid:
            self.mode, self.normal_after, self.source_session = "custom", None, None
            self.normal_operations.clear()
            self.last_phone_receipt = None
            self.normal_grant = self.proven_phone = None
            # Revoke admission only. A queued replay of an older own point could
            # otherwise create extra zone transitions before the latest Store target.
        return valid

    async def _ensure_registration(self):
        if self.registration_guard is None:
            return True
        try:
            valid = await self.registration_guard() is True
        except Exception:
            valid = False
        if not valid:
            self._fail_native()
            self.normal_operations.clear()
        return valid

    def _status(self):
        verified_own = (isinstance(self.last_custom_receipt, dict)
                        and self.last_custom_receipt.get("verified") is True
                        and self.last_custom_receipt.get("origin") == "custom"
                        and self.last_custom_receipt.get("person_id") == self.person_id)
        return {"mode": self._mode(), "person_id": self.person_id,
                "remaining_seconds": max(0, self.normal_after - self.clock()) if self._mode() == "warmup" else 0,
                "last_custom_receipt": self.last_custom_receipt,
                "last_phone_receipt": self.last_phone_receipt,
                "replay_required": self.replay_required,
                "upstream_uncertain": self.upstream_uncertain,
                "uncertainty_origin": self.uncertainty_origin,
                "recovery_phase": self.pending_custom["phase"] if self.pending_custom else None,
                "recovery_retryable": self._recoverable(),
                "custom_point_digest": _custom_point_digest(self.last_custom, self.person_id) if verified_own else None,
                "scheduled_return_supported": True,
                "scheduled_return": dict(self.scheduled_return) if self.scheduled_return is not None else None}

    def _receipt(self, origin, *, replay=False, recovery=False):
        self.sequence += 1
        result = {"origin": origin, "person_id": self.person_id,
                  "sequence": self.sequence, "verified": True, "replay": replay}
        if recovery:
            result["recovery"] = True
        return result

    def _begin_phone(self):
        if self._expire_return():
            return None
        self._begin_native()
        admission = {"grant": self.normal_grant, "source_session": self.source_session,
                     "lower": self.wall_clock(), "monotonic": self.clock()}
        if self._expire_return():
            # This marker is ours and no forward has started. Its clear must be durable.
            self._commit_native()
            return None
        return admission

    async def _finish_phone(self, point, admission):
        metadata = None
        if self.phone_snapshot is not None:
            try:
                snapshots = await self.phone_snapshot(dict(point))
            except (Exception, asyncio.CancelledError) as error:
                self._fail_native()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise RuntimeError("phone snapshot unavailable") from None
            upper, elapsed = self.wall_clock(), self.clock() - admission["monotonic"]
            lower = admission["lower"]
            bounds_valid = (all(type(value) in {int, float} and math.isfinite(value) for value in (lower, upper, elapsed))
                            and upper >= lower and elapsed >= 0 and abs((upper - lower) - elapsed) <= 1)
            valid = (bounds_valid and isinstance(snapshots, dict) and len(snapshots) == 2
                     and self.person_id in snapshots and isinstance(admission["grant"], str)
                     and isinstance(admission["source_session"], str))
            proven = self.proven_phone
            reusable = (proven is not None and proven["grant"] == admission["grant"]
                        and proven["source_session"] == admission["source_session"] and proven["point"] == point)
            if valid:
                for entity_id, snapshot in snapshots.items():
                    try:
                        if (not isinstance(entity_id, str) or len(entity_id) > 256
                                or not isinstance(snapshot, dict) or set(snapshot) != {"last_updated", "context_id"}
                                or not isinstance(snapshot["last_updated"], str) or len(snapshot["last_updated"]) > 64
                                or not isinstance(snapshot["context_id"], str)
                                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", snapshot["context_id"])):
                            valid = False
                            break
                        timestamp = datetime.fromisoformat(snapshot["last_updated"].replace("Z", "+00:00"))
                        if timestamp.tzinfo is None:
                            valid = False
                            break
                        fresh = lower <= timestamp.timestamp() <= upper
                        reused = reusable and proven["snapshots"].get(entity_id) == snapshot
                        if not fresh and not reused:
                            valid = False
                            break
                    except (ValueError, TypeError, OverflowError):
                        valid = False
                        break
            if valid:
                payload = {"operation_id": uuid.uuid4().hex, "grant": admission["grant"],
                           "source_session": admission["source_session"], "point": dict(point),
                           "snapshots": snapshots, "lower": lower, "upper": upper, "elapsed": elapsed}
                try:
                    result = await self.record_phone(payload)
                    if (not isinstance(result, dict) or set(result) != {"history_id", "native_timestamp"}
                            or not isinstance(result["history_id"], str)
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result["history_id"])
                            or result["native_timestamp"] != snapshots[self.person_id]["last_updated"]):
                        raise RuntimeError("phone history acknowledgment invalid")
                except (Exception, asyncio.CancelledError) as error:
                    # Callback input/schema/database faults are native uncertainty, not phone input errors.
                    self._fail_native()
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    raise RuntimeError("phone history capture failed") from None
                metadata = {**result, "grant": admission["grant"]}
                if self.normal_grant == admission["grant"] and self.source_session == admission["source_session"]:
                    self.proven_phone = {"grant": admission["grant"], "source_session": admission["source_session"],
                                         "point": dict(point), "snapshots": snapshots}
        try:
            self._commit_native()
        except Exception:
            self._fail_native()
            raise
        self.last_phone_receipt = {**self._receipt("phone"), **(metadata or {})}
        self._expire_return()

    async def _read_json(self, request):
        if request.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("encoding")
        body = await request.read()
        if len(body) > self.max_body_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=self.max_body_bytes, actual_size=len(body))
        return _json(body)

    def _operation(self, key, point):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
            raise ValueError("operation key")
        return {"key": key, "digest": hashlib.sha256(json.dumps(point, sort_keys=True).encode()).hexdigest()}

    def _begin_native(self, *, point=None, operation=None, recovery=False):
        if self.upstream_uncertain and not (recovery and self._recoverable()
                and self.pending_custom["point"] == point and self.pending_custom["operation"] == operation):
            raise RuntimeError("upstream reconciliation required")
        previous = self.pending_custom
        self.upstream_uncertain = True
        self.uncertainty_origin = "custom" if point is not None else "phone"
        pending = {"point": point, "operation": operation, "phase": "own_location_inflight"} if point is not None else None
        self.pending_custom = pending
        try:
            self._save(last_custom=self.last_custom, operation=self.last_operation,
                       receipt=self.last_custom_receipt, pending=pending)
        except Exception:
            if recovery:
                # No new POST can have started: preserve the live process's
                # known closed intent; a crash still sees the conservative file.
                self.pending_custom = previous
            raise

    def _commit_native(self, *, point=None, operation=None, receipt=None):
        self._save(last_custom=self.last_custom if point is None else point,
                   operation=self.last_operation if operation is None else operation,
                   receipt=self.last_custom_receipt if receipt is None else receipt,
                   pending=None, upstream_uncertain=False)
        self.upstream_uncertain = False
        self.uncertainty_origin = self.pending_custom = None

    async def _phase(self, phase):
        if self.uncertainty_origin != "custom" or not self.pending_custom:
            raise RuntimeError("missing own intent")
        previous = self.pending_custom["phase"]
        allowed = {"own_location_inflight": {"location_closed"},
                   "location_closed": {"checkpoint_inflight"},
                   "checkpoint_inflight": {"checkpoint_closed"},
                   "checkpoint_closed": {"checkpoint_inflight"}}
        if phase not in allowed.get(previous, set()):
            raise RuntimeError("invalid native phase transition")
        pending = {**self.pending_custom, "phase": phase}
        if phase in {"location_closed", "checkpoint_closed"}:
            # A full correlated response is already known in this process.
            # If this metadata write fails, RAM may retry once disk heals;
            # a restart sees the old inflight file and remains blocked.
            self.pending_custom = pending
        self._save(last_custom=self.last_custom, operation=self.last_operation,
                   receipt=self.last_custom_receipt, pending=pending)
        self.pending_custom = pending

    def _fail_native(self):
        self.mode, self.normal_after, self.last_phone_receipt = "custom", None, None
        self.source_session = None
        self.normal_grant = self.proven_phone = None
        # The marker was fsynced before forwarding; never clear it on an error.

    async def _publish_custom(self, point, operation, *, replay=False, recovery=False):
        if not await self._ensure_registration():
            raise RuntimeError("registration changed or unavailable")
        returning = self.scheduled_return is not None and self.scheduled_return["state"] in {"due", "applied"}
        if returning and not self.upstream_uncertain and await self.verify_native(dict(point)):
            # Preserve Recorder state IDs when Own is already native (including pending DELETE).
            self._begin_native(point=point, operation=operation)
            await self._phase("location_closed")
            return await self._finish_custom(point, replay=replay)
        self._begin_native(point=point, operation=operation, recovery=recovery)
        async with self.session.post(self.backend_url + "/api/webhook/" + self.webhook_id,
                                     json=_payload(point), allow_redirects=False,
                                     **self.backend_request_options) as response:
            await response.read()
            if not 200 <= response.status < 300:
                raise RuntimeError("native publication failed")
        await self._phase("location_closed")
        return await self._finish_custom(point, replay=replay, recovery=recovery)

    async def _finish_custom(self, point, *, replay=False, recovery=False):
        if not await self._ensure_registration():
            raise RuntimeError("registration changed or unavailable")
        if not await self.verify_native(dict(point)):
            raise RuntimeError("native readback failed")
        closed = False
        async def checkpoint_phase(phase):
            nonlocal closed
            if phase not in {"checkpoint_inflight", "checkpoint_closed"}:
                raise RuntimeError("invalid checkpoint phase")
            await self._phase(phase)
            closed = phase == "checkpoint_closed"
        if await self.checkpoint_native(dict(point), phase=checkpoint_phase) is False:
            raise RuntimeError("native checkpoint failed")
        if not closed:
            raise RuntimeError("checkpoint callback did not close its RPC")
        if not await self.verify_native(dict(point)):
            raise RuntimeError("native post-checkpoint readback failed")
        return self._receipt("custom", replay=replay, recovery=recovery)

    def _complete_custom(self, point, operation, receipt):
        self._commit_native(point=point, operation=operation, receipt=receipt)
        self.last_custom, self.last_operation = point, operation
        self.last_custom_receipt, self.replay_required = receipt, False
        self.last_phone_receipt = None
        self.normal_operations.clear()
        self._apply_return()

    async def recover_pending_custom(self):
        async with self.lock:
            if not self._recoverable():
                return None
            self._fail_native()
            self.recovery_active = True
            try:
                if not await self.ready_native():
                    return None
                intent = self.pending_custom
                point, operation = intent["point"], intent["operation"]
                if await self.verify_native(dict(point)):
                    receipt = await self._finish_custom(point, recovery=True)
                else:
                    receipt = await self._publish_custom(point, operation, recovery=True)
                self._complete_custom(point, operation, receipt)
                return receipt
            except (Exception, asyncio.CancelledError) as error:
                self._fail_native()
                if isinstance(error, asyncio.CancelledError):
                    raise
                return None
            finally:
                self.recovery_active = False

    async def replay_last_custom(self):
        async with self.lock:
            self.mode, self.normal_after, self.last_phone_receipt = "custom", None, None
            self.source_session = self.normal_grant = self.proven_phone = None
            if self.last_custom is None or self.upstream_uncertain:
                return None
            try:
                if not await self.ready_native():
                    self.replay_required = True
                    return None
                receipt = await self._publish_custom(self.last_custom, self.last_operation, replay=True)
                self._complete_custom(self.last_custom, self.last_operation, receipt)
                return receipt
            except (Exception, asyncio.CancelledError) as error:
                self._fail_native()
                self.replay_required = True
                if isinstance(error, asyncio.CancelledError):
                    raise
                return None

    async def _control(self, request):
        if not self._authorized(request):
            return web.json_response({"error": "control authentication required"}, status=401)
        if request.path in {"/health", "/control/status"} and request.method == "GET":
            await self._ensure_source_session()
            return web.json_response(self._status())
        if request.path in {"/control/return", "/control/return/ack"} and request.method == "POST":
            return await self._control_return(request)
        if request.method != "POST" or request.path not in {"/control/custom", "/control/normal"}:
            return web.json_response({"error": "unsupported control"}, status=405)
        body = await self._read_json(request)
        if not isinstance(body, dict) or set(body) - {"idempotency_key", "point", "source_session", "expected_id"}:
            raise ValueError("control schema")
        if "expected_id" in body and body["expected_id"] is not None and not self._return_id(body["expected_id"]):
            raise ValueError("scheduled return generation")
        if request.path != "/control/normal" and "expected_id" in body:
            raise ValueError("custom schema")
        key = body.get("idempotency_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
            raise ValueError("idempotency key required")
        if self.recovery_active or self._recoverable() or (request.path == "/control/normal" and self.upstream_uncertain):
            return web.json_response({"error": "own recovery pending or upstream uncertain", **self._status()}, status=503)
        async with self.lock:
            if self.upstream_uncertain:
                self._fail_native()
                return web.json_response({"error": "upstream reconciliation required", **self._status()}, status=503)
            if request.path == "/control/normal":
                if "point" in body:
                    raise ValueError("normal schema")
                if not await self._ensure_registration():
                    return web.json_response({"error": "registration changed or unavailable", **self._status()}, status=503)
                source_session = body.get("source_session")
                if self.source_session_check is not None:
                    if not isinstance(source_session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source_session):
                        raise ValueError("source session required")
                    if not await self.source_session_check(source_session):
                        self._fail_native()
                        return web.json_response({"error": "source session unavailable", **self._status()}, status=503)
                    if key in self.normal_operations and source_session != self.source_session:
                        return web.json_response({"error": "normal session conflict"}, status=409)
                # Validation above awaits HA; expiry/CAS must be checked again afterwards.
                self._expire_return()
                if self._return_dirty:
                    return web.json_response({"error": "scheduled return persistence pending", **self._status()}, status=503)
                expected = self.scheduled_return["id"] if self.scheduled_return is not None else None
                if (body.get("expected_id") != expected or (
                        self.scheduled_return is not None and self.scheduled_return["state"] in {"due", "applied"})):
                    return web.json_response({"error": "scheduled return changed", **self._status()}, status=409)
                if key not in self.normal_operations:
                    self.mode, self.normal_after = "warmup", self.clock() + self.warmup_seconds
                    self.source_session = source_session
                    self.last_phone_receipt = None
                    self.normal_grant, self.proven_phone = key, None
                    self.normal_operations[key] = self.normal_after
                return web.json_response(self._status(), status=202)
            if self.source_control_check is not None:
                source_session = body.get("source_session")
                if not isinstance(source_session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source_session):
                    raise ValueError("source session required")
                if not await self.source_control_check(source_session):
                    return web.json_response({"error": "source control session unavailable", **self._status()}, status=503)
            self.mode, self.normal_after, self.last_phone_receipt = "custom", None, None
            self.source_session = None
            self.normal_grant = self.proven_phone = None
            self.normal_operations.clear()
            if "source_session" in body and self.source_control_check is None:
                raise ValueError("custom schema")
            if "point" not in body:
                return web.json_response(self._status())
            if not await self._ensure_registration():
                return web.json_response({"error": "registration changed or unavailable", **self._status()}, status=503)
            point = _point(body["point"], self.person_id)
            operation = self._operation(key, point)
            if self.last_operation and self.last_operation["key"] == key:
                if self.last_operation != operation:
                    return web.json_response({"error": "idempotency conflict"}, status=409)
                if not self.replay_required and await self.verify_native(dict(point)):
                    return web.json_response({"receipt": self.last_custom_receipt, "idempotent": True})
            receipt = await self._publish_custom(point, operation)
            self._complete_custom(point, operation, receipt)
            return web.json_response({"receipt": receipt, "idempotent": False})

    async def _control_return(self, request):
        ack = request.path == "/control/return/ack"
        body = await self._read_json(request)
        required = {"idempotency_key", "source_session", "expected_id"} | (set() if ack else {"at"})
        if (not isinstance(body, dict) or set(body) != required or not self._return_id(body["idempotency_key"])
                or not self._return_id(body["source_session"])
                or (body["expected_id"] is not None and not self._return_id(body["expected_id"]))
                or (ack and body["expected_id"] is None)):
            raise ValueError("scheduled return schema")
        at = body.get("at")
        if at is not None and (type(at) is not int or not 0 <= at <= 253402300799):
            raise ValueError("scheduled return UTC seconds")
        digest = hashlib.sha256(json.dumps({"ack": ack, "at": at, "expected_id": body["expected_id"]},
                                           sort_keys=True).encode()).hexdigest()
        async with self.lock:
            if self.source_control_check is not None and not await self.source_control_check(body["source_session"]):
                return web.json_response({"error": "source control session unavailable"}, status=503)
            self._expire_return()
            current_id = self.scheduled_return["id"] if self.scheduled_return is not None else None
            previous = self.return_receipt
            if previous is not None and previous["key"] == body["idempotency_key"]:
                if previous["digest"] != digest or previous["to_id"] != current_id:
                    return web.json_response({"error": "idempotency conflict"}, status=409)
                if self._return_dirty:
                    self._save_return()
                return web.json_response(self._status())
            if current_id != body["expected_id"]:
                return web.json_response({"error": "scheduled return changed", **self._status()}, status=409)
            if (self.upstream_uncertain or self.recovery_active or self._recoverable()
                    or self.replay_required or self.last_custom is None or not self.last_custom_receipt
                    or self.last_custom_receipt.get("verified") is not True):
                return web.json_response({"error": "own recovery pending", **self._status()}, status=503)
            state = self.scheduled_return["state"] if self.scheduled_return is not None else None
            if ack:
                if state != "applied" or self.mode != "custom" or not await self.verify_native(dict(self.last_custom)):
                    return web.json_response({"error": "scheduled return not applied", **self._status()}, status=409)
            elif state in {"due", "applied"}:
                return web.json_response({"error": "scheduled return awaiting acknowledgment", **self._status()}, status=409)
            elif at is not None:
                now = self.wall_clock()
                if type(now) not in {int, float} or not math.isfinite(now) or now < 0:
                    raise RuntimeError("scheduled return clock unavailable")
                if at <= now:
                    raise ValueError("scheduled return must be future UTC")
            new_id = uuid.uuid4().hex
            self.scheduled_return = {"id": new_id, "at": at, "state": "armed" if at is not None else "idle"}
            self.return_receipt = {"key": body["idempotency_key"], "digest": digest,
                                   "from_id": current_id, "to_id": new_id}
            self._return_dirty = True
            try:
                self._set_return_clock()
            except ValueError:
                self._fail_native()
                raise RuntimeError("scheduled return clock unavailable") from None
            self._save_return()
            return web.json_response(self._status())

    def _classify(self, body):
        if not isinstance(body, dict) or set(body) - {"type", "data"}:
            raise ValueError("webhook schema")
        kind = body.get("type")
        if kind == "update_location":
            data = body.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("gps"), list) or len(data["gps"]) != 2:
                raise ValueError("location schema")
            point = _point({"latitude": data["gps"][0], "longitude": data["gps"][1],
                            "gps_accuracy": data.get("gps_accuracy", 0)}, self.person_id)
            return "location", _payload(point), point
        if kind in {"get_config", "get_zones"} and body.get("data") in (None, {}):
            return "read", {"type": kind}, None
        if kind in {"register_sensor", "update_sensor_states"}:
            supplied = body.get("data")
            values = supplied if isinstance(supplied, list) else [supplied]
            safe = []
            for sensor in values:
                if not isinstance(sensor, dict):
                    continue
                sensor_id = sensor.get("unique_id")
                spec = ACTIVITY_SENSORS.get(sensor_id) if isinstance(sensor_id, str) else None
                if spec is None or sensor.get("type") != spec[0]:
                    continue
                value = sensor.get("state")
                if (sensor_id == "detected_activity" and (not isinstance(value, str) or value not in ACTIVITY_VALUES)
                        or sensor_id != "detected_activity" and type(value) is not bool):
                    continue
                clean = {"unique_id": sensor_id, "type": spec[0], "state": value}
                if kind == "register_sensor":
                    if isinstance(supplied, list):
                        continue
                    clean["name"] = spec[1]
                    if type(sensor.get("disabled")) is bool:
                        clean["disabled"] = sensor["disabled"]
                safe.append(clean)
            if safe:
                return "activity", {"type": kind, "data": safe[0] if kind == "register_sensor" else safe}, None
            return "blocked", None, None
        if kind == "fire_event":
            return "blocked", None, None
        if kind == "update_registration" and self.production_mode:
            supplied = body.get("data")
            if not isinstance(supplied, dict):
                raise ValueError("registration schema")
            clean = {}
            for field in ("app_version", "device_name", "manufacturer", "model", "os_version"):
                value = supplied.get(field)
                if value is None and field == "os_version":
                    continue
                if not isinstance(value, str) or not value or len(value) > 256:
                    raise ValueError("registration metadata")
                clean[field] = value
            if "app_data" in supplied:
                app_data = supplied["app_data"]
                if not isinstance(app_data, dict):
                    raise ValueError("registration app data")
                safe = {}
                if "push_websocket_channel" in app_data:
                    if type(app_data["push_websocket_channel"]) is not bool:
                        raise ValueError("registration push channel")
                    safe["push_websocket_channel"] = app_data["push_websocket_channel"]
                if "push_url" in app_data or "push_token" in app_data:
                    token = app_data.get("push_token")
                    if (self.allowed_push_url is None or app_data.get("push_url") != self.allowed_push_url
                            or not isinstance(token, str) or not token or len(token) > 8192):
                        raise ValueError("registration push provider")
                    safe.update(push_url=self.allowed_push_url, push_token=token)
                clean["app_data"] = safe
            return "registration", {"type": kind, "data": clean}, None
        # Registration/update_registration need an explicit setup contract; lab denies both.
        raise ValueError("unsupported webhook type")

    def _rewrite(self, value, registration_id=None):
        if self.production_mode:
            return value
        registration_id = registration_id or self.webhook_id
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in {"internal_url", "external_url", "remote_ui_url"}:
                    result[key] = self.public_url
                elif key == "cloudhook_url":
                    result[key] = self.public_url + "/api/webhook/" + registration_id
                else:
                    result[key] = self._rewrite(item, registration_id)
            return result
        if isinstance(value, list):
            return [self._rewrite(item, registration_id) for item in value]
        if isinstance(value, str):
            if value.startswith(("{", "[")):
                with contextlib.suppress(ValueError):
                    return json.dumps(self._rewrite(_json(value), registration_id), separators=(",", ":"))
            return value.replace(self.backend_url, self.public_url)
        return value

    async def _http_forward(self, request, body=None, *, registration_id=None):
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in {"host", "connection", "content-length", "transfer-encoding", "accept-encoding"}}
        kwargs = {"json": body} if body is not None else {"data": await request.read()}
        async with self.session.request(request.method, self.backend_url + request.rel_url.path_qs,
                                        headers=headers, allow_redirects=False,
                                        **self.backend_request_options, **kwargs) as response:
            data = await response.read()
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            if not self.production_mode:
                try:
                    data = json.dumps(self._rewrite(_json(data), registration_id), separators=(",", ":")).encode()
                except (ValueError, UnicodeError):
                    data = data.replace(self.backend_url.encode(), self.public_url.encode())
            returned_headers = {k: self._rewrite(v, registration_id) for k, v in response.headers.items()
                                if k.lower() not in {"content-length", "content-encoding", "transfer-encoding",
                                                     "connection", "location", "etag"}}
            returned_headers["Content-Type"] = content_type
            if "Location" in response.headers:
                returned_headers["Location"] = self._rewrite(response.headers["Location"], registration_id)
            return response.status, data, returned_headers

    async def _handle(self, request):
        try:
            if request.path == "/health" or request.path.startswith("/control/"):
                return await self._control(request)
            if self._authorized(request):
                return web.json_response({"error": "control token is not HA authentication"}, status=403)
            if "%" in request.path or "\\" in request.path or ".." in request.path.split("/"):
                raise ValueError("noncanonical path")
            if request.path == "/api/websocket":
                return await self._websocket(request)
            if request.path.startswith("/api/mobile_app/registrations"):
                return web.json_response({"error": "explicit setup required"}, status=403)
            if request.path.startswith("/api/webhook/"):
                registration_id = request.path.removeprefix("/api/webhook/")
                if registration_id not in self.passthrough_webhooks | {self.webhook_id} or request.query:
                    return web.json_response({"error": "unprotected registration denied"}, status=403)
                if request.method not in {"POST", "PUT"}:
                    return web.json_response({"error": "method denied"}, status=405)
                supplied = await self._read_json(request)
                if registration_id != self.webhook_id:
                    status, data, headers = await self._http_forward(request, supplied, registration_id=registration_id)
                    return web.Response(status=status, body=data, headers=headers)
                kind, body, point = self._classify(supplied)
                async with self.lock:
                    if kind == "location":
                        await self._ensure_source_session()
                    if kind == "registration" and not await self._ensure_registration():
                        return web.json_response({"error": "registration changed or unavailable"}, status=503)
                    if kind == "blocked" or (kind == "location" and self._mode() != "normal"):
                        return web.json_response({} if self.production_mode else {"blocked": True, "mode": self._mode()})
                    if kind == "location":
                        admission = self._begin_phone()
                        if admission is None:
                            return web.json_response({} if self.production_mode else {"blocked": True, "mode": self._mode()})
                    status, data, headers = await self._http_forward(request, body)
                    if kind == "location":
                        if not 200 <= status < 300 or not await self.verify_native(dict(point)):
                            raise RuntimeError("phone readback failed")
                        await self._finish_phone(point, admission)
                    return web.Response(status=status, body=data, headers=headers)
            # Ordinary UI reads and our panel remain available; no generic REST write tunnel.
            if (not self.production_mode and request.method not in {"GET", "HEAD"}
                    and not request.path.startswith(("/auth/", "/api/manual_location/"))):
                return web.json_response({"error": "REST mutation denied"}, status=403)
            status, data, headers = await self._http_forward(request)
            return web.Response(status=status, body=data, headers=headers)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "body too large"}, status=413)
        except ClientError:
            # TLS certificate errors also inherit ValueError; they are upstream
            # failures, never malformed phone input or grounds to keep a grant.
            self._fail_native()
            return web.json_response({"error": "gateway operation failed"}, status=503)
        except (ValueError, UnicodeError, TypeError, KeyError):
            return web.json_response({"error": "invalid or unsupported input"}, status=400)
        except (Exception, asyncio.CancelledError) as error:
            self._fail_native()
            if isinstance(error, asyncio.CancelledError):
                raise
            return web.json_response({"error": "gateway operation failed"}, status=503)

    async def _websocket(self, request):
        downstream = web.WebSocketResponse(max_msg_size=self.max_ws_message_bytes, compress=False)
        await downstream.prepare(request)
        pending = {}
        forwarded_headers = {key: value for key, value in request.headers.items() if key.lower() in {
            "host", "origin", "x-forwarded-for", "x-real-ip", "x-forwarded-proto", "x-forwarded-host"
        }} if self.production_mode else None
        try:
            async with self.session.ws_connect(self.backend_url + "/api/websocket",
                                               max_msg_size=self.max_ws_message_bytes, compress=0,
                                               headers=forwarded_headers,
                                               **self.backend_request_options) as upstream:
                async def receive_backend():
                    try:
                        async for message in upstream:
                            if self.production_mode and message.type == WSMsgType.BINARY:
                                await downstream.send_bytes(message.data)
                                continue
                            if message.type != WSMsgType.TEXT:
                                await downstream.close(code=1003)
                                return
                            try:
                                raw = _json(message.data)
                                entry = pending.get(raw.get("id")) if isinstance(raw, dict) else None
                                if entry is not None:
                                    future, registration_id = entry
                                    response = self._rewrite(raw, registration_id)
                                    if not future.done():
                                        future.set_result(response)
                                else:
                                    await downstream.send_json(self._rewrite(raw))
                            except (ValueError, TypeError):
                                await downstream.close(code=1007)
                                return
                    finally:
                        for future, _registration_id in tuple(pending.values()):
                            if not future.done():
                                future.set_exception(ConnectionError("backend WebSocket closed"))
                        if not downstream.closed:
                            await downstream.close(code=upstream.close_code if upstream.close_code in {1000, 1001} else 1011)
                receiver = asyncio.create_task(receive_backend())
                try:
                    async for message in downstream:
                        if self.production_mode and message.type == WSMsgType.BINARY:
                            await upstream.send_bytes(message.data)
                            continue
                        if message.type != WSMsgType.TEXT:
                            await downstream.close(code=1003)
                            break
                        try:
                            commands = _json(message.data)
                            commands = commands if isinstance(commands, list) else [commands]
                            if not commands:
                                raise ValueError("empty batch")
                            for command in commands:
                                await self._ws_command(command, downstream, upstream, pending)
                        except (ValueError, TypeError, KeyError):
                            await downstream.close(code=1007)
                            break
                finally:
                    receiver.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await receiver
        except (Exception, asyncio.CancelledError) as error:
            self._fail_native()
            if isinstance(error, asyncio.CancelledError):
                raise
            await downstream.close(code=1011)
        return downstream

    async def _ws_command(self, command, downstream, upstream, pending):
        if not isinstance(command, dict):
            raise ValueError("command schema")
        kind, identifier = command.get("type"), command.get("id")
        if kind == "auth":
            if hmac.compare_digest(str(command.get("access_token", "")), self.control_token):
                await downstream.close(code=1008)
                return
            await upstream.send_json(command)
            return
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
            raise ValueError("command id")
        async def denied():
            await downstream.send_json({"id": identifier, "type": "result", "success": False,
                                        "error": {"code": "gateway_denied", "message": "Operation denied"}})
        if kind == "webhook/handle":
            registration_id = command.get("webhook_id")
            if registration_id in self.denied_webhooks:
                return await denied()
            if self.production_mode and registration_id not in self.passthrough_webhooks | {self.webhook_id}:
                # Existing non-mobile webhooks retain Core's authorization/schema.
                # New mobile registrations are separately denied by the public route.
                await upstream.send_json(command)
                return
            if (registration_id not in self.passthrough_webhooks | {self.webhook_id}
                    or command.get("method") not in {"POST", "PUT"} or command.get("query")
                    or set(command) - {"id", "type", "webhook_id", "method", "body", "headers", "query"}):
                return await denied()
            headers = command.get("headers", {})
            if not isinstance(headers, dict) or any(k.lower() == "content-encoding" and v != "identity" for k, v in headers.items()):
                return await denied()
            raw_body = command.get("body", "")
            if not isinstance(raw_body, str) or len(raw_body.encode("utf-8")) > self.max_body_bytes:
                return await denied()
            supplied = _json(raw_body)
            category, body, point = self._classify(supplied) if registration_id == self.webhook_id else ("passthrough", supplied, None)
            async with self.lock:
                if category == "location":
                    await self._ensure_source_session()
                if category == "registration" and not await self._ensure_registration():
                    return await denied()
                if category == "blocked" or (category == "location" and self._mode() != "normal"):
                    await downstream.send_json({"id": identifier, "type": "result", "success": True,
                        "result": {"status": 200, "body": '{}' if self.production_mode else '{"blocked":true}',
                                   "headers": {"Content-Type": "application/json"}}})
                    return
                if identifier in pending:
                    return await denied()
                future = asyncio.get_running_loop().create_future()
                pending[identifier] = (future, registration_id)
                try:
                    if category == "location":
                        admission = self._begin_phone()
                        if admission is None:
                            await downstream.send_json({"id": identifier, "type": "result", "success": True,
                                "result": {"status": 200, "body": '{}' if self.production_mode else '{"blocked":true}',
                                           "headers": {"Content-Type": "application/json"}}})
                            return
                    await upstream.send_json({**command, "body": json.dumps(body)})
                    response = await asyncio.wait_for(future, self.upstream_timeout_seconds)
                    if category == "location":
                        status = response.get("result", {}).get("status", 0)
                        if not response.get("success") or not 200 <= status < 300 or not await self.verify_native(dict(point)):
                            raise RuntimeError("phone WS readback failed")
                        await self._finish_phone(point, admission)
                    await downstream.send_json(response)
                finally:
                    pending.pop(identifier, None)
            return
        allowed = {"supported_features", "get_states", "get_config", "get_services", "subscribe_events",
                   "subscribe_entities", "unsubscribe_events", "ping", "lovelace/config"}
        if self.production_mode or kind in allowed or (isinstance(kind, str) and kind.startswith("manual_location/")):
            await upstream.send_json(command)
        else:
            await denied()
