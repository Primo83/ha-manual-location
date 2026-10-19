"""Optional loopback control client; no phone data or credentials reach the panel."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from urllib.parse import urlsplit

from aiohttp import ClientTimeout

from homeassistant.core import valid_entity_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_UNSET = object()


class GatewayError(Exception):
    """The independent gateway has not confirmed the requested operation."""


class GatewayBridge:
    """Control one protected person, with a fresh, RAM-only HA session epoch."""

    def __init__(self, hass, entry, settings):
        if not isinstance(settings, dict) or set(settings) != {"url", "token", "person_id"}:
            raise GatewayError("invalid gateway configuration")
        url, token, person = settings["url"], settings["token"], settings["person_id"]
        try:
            parsed = urlsplit(url)
            valid_url = (parsed.scheme in {"http", "https"} and parsed.hostname == "127.0.0.1"
                         and parsed.port is not None and parsed.username is None and parsed.password is None
                         and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
        except (AttributeError, TypeError, ValueError):
            valid_url = False
        if (not valid_url or not isinstance(token, str) or not 16 <= len(token) <= 512
                or any(not 33 <= ord(char) <= 126 for char in token)
                or not isinstance(person, str) or not person.startswith("person.") or not valid_entity_id(person)):
            raise GatewayError("invalid gateway configuration")
        self.hass, self.entry_id = hass, entry.entry_id
        self.url, self._token, self.person_id = url.rstrip("/"), token, person
        self.session = secrets.token_urlsafe(32)
        self.ready = False
        self.pending = False
        self.return_supported = False
        self.handoff_supported = False

    def invalidate(self):
        """A delayed normal grant must not survive a later close or failure."""
        self.ready = False
        self.session = secrets.token_urlsafe(32)

    @classmethod
    def from_entry(cls, hass, entry):
        if "gateway" not in entry.options:
            return None
        return cls(hass, entry, entry.options["gateway"])

    def key(self, revision, kind, current=None):
        value = [self.entry_id, self.person_id, revision, kind, current]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def custom_point_digest(self, current):
        if current is None:
            return None
        value = {"person_id": self.person_id, "gps_accuracy": 0,
                 **{key: float(current[key]) or 0.0 for key in ("latitude", "longitude")}}
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()

    async def _request(self, path, body=None):
        try:
            session = async_get_clientsession(self.hass)
            async with session.request(
                "GET" if body is None else "POST", self.url + path,
                headers={"Authorization": "Bearer " + self._token}, json=body,
                timeout=ClientTimeout(total=15), allow_redirects=False,
            ) as response:
                if response.status not in {200, 202}:
                    raise GatewayError("gateway operation unavailable")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > 16384:
                        raise GatewayError("invalid gateway response")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise GatewayError("invalid gateway response")
                return result
        except Exception:
            raise GatewayError("gateway operation unavailable") from None

    def _receipt(self, value, origin):
        if value is None:
            return None
        if (not isinstance(value, dict) or value.get("person_id") != self.person_id
                or value.get("origin") != origin or value.get("verified") is not True
                or type(value.get("sequence")) is not int or value["sequence"] < 0
                or any(key in value and type(value[key]) is not bool for key in ("replay", "recovery"))):
            raise GatewayError("invalid gateway identity")
        metadata = {"history_id", "grant", "native_timestamp"}
        if metadata.intersection(value):
            if (origin != "phone" or not metadata.issubset(value)
                    or any(not isinstance(value[key], str) or not 1 <= len(value[key]) <= 128 for key in metadata)):
                raise GatewayError("invalid history receipt")
            from datetime import datetime

            try:
                instant = datetime.fromisoformat(value["native_timestamp"])
                if instant.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise GatewayError("invalid history receipt") from None
        return {key: value[key] for key in ("origin", "person_id", "sequence", "verified", "replay", "recovery", *metadata) if key in value}

    def _status(self, result):
        if result.get("person_id") != self.person_id or result.get("mode") not in {"custom", "warmup", "normal"}:
            raise GatewayError("invalid gateway identity")
        remaining = result.get("remaining_seconds", 0)
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
            raise GatewayError("invalid gateway status")
        custom = self._receipt(result.get("last_custom_receipt"), "custom")
        phone = self._receipt(result.get("last_phone_receipt"), "phone")
        supported = result.get("scheduled_return_supported", False)
        plan = result.get("scheduled_return")
        if type(supported) is not bool:
            raise GatewayError("invalid scheduled return status")
        if supported and "scheduled_return" not in result:
            raise GatewayError("invalid scheduled return status")
        if plan is not None:
            if (not supported or not isinstance(plan, dict) or set(plan) != {"id", "at", "state"}
                    or not isinstance(plan["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", plan["id"])
                    or not isinstance(plan["state"], str) or plan["state"] not in {"idle", "armed", "due", "applied"}
                    or (plan["state"] == "idle" and plan["at"] is not None)
                    or (plan["state"] != "idle" and (type(plan["at"]) is not int or not 0 < plan["at"] <= 253402300799))):
                raise GatewayError("invalid scheduled return status")
            plan = dict(plan)
        digest = result.get("custom_point_digest")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise GatewayError("invalid custom point digest")
        self.return_supported = self.return_supported or supported
        handoff_supported = result.get("handoff_supported", False)
        handoff = result.get("handoff")
        if type(handoff_supported) is not bool or (handoff is not None and (
                not handoff_supported or not isinstance(handoff, dict) or set(handoff) != {"id", "state"}
                or not isinstance(handoff["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", handoff["id"])
                or handoff["state"] not in {"idle", "prepared", "applied"})):
            raise GatewayError("invalid handoff status")
        self.handoff_supported = self.handoff_supported or handoff_supported
        blocked = self.pending or result.get("upstream_uncertain") or result.get("replay_required") or result.get("recovery_retryable")
        return {"enabled": True, "available": True, "person_id": self.person_id,
                "mode": "blocked" if blocked else result["mode"],
                "gps_blocked": result["mode"] in {"custom", "warmup"} and not bool(result.get("upstream_uncertain")),
                "remaining_seconds": max(0, math.ceil(remaining)), "pending": self.pending,
                "replay_required": bool(result.get("replay_required")),
                "upstream_uncertain": bool(result.get("upstream_uncertain")),
                "recovery_retryable": bool(result.get("recovery_retryable")),
                "has_custom_point": custom is not None,
                "last_custom_receipt": custom, "last_phone_receipt": phone,
                "scheduled_return_supported": supported, "scheduled_return": plan,
                "custom_point_digest": digest,
                "handoff_supported": handoff_supported, "handoff": handoff}

    async def handoff(self, key, expected_id):
        self.invalidate()
        result = await self._request("/control/handoff", {
            "idempotency_key": key, "source_session": self.session, "expected_id": expected_id})
        status = self._status(result)
        if not status["handoff"] or status["handoff"]["state"] == "idle":
            if (status["mode"] != "custom" or not status["gps_blocked"]
                    or status["replay_required"] or status["recovery_retryable"]):
                raise GatewayError("handoff unavailable")
            # No phone was admitted after the last explicit Own. The gate has
            # proved a clean close; the existing Store timestamp stays intact.
            self.ready, self.pending = True, False
            return None
        return await self.read_handoff()

    async def read_handoff(self):
        result = await self._request("/control/handoff")
        handoff = result.get("handoff")
        if handoff is None or handoff.get("state") == "idle":
            return handoff
        from .coordinator import coordinates, timestamp

        try:
            if (not isinstance(handoff, dict) or handoff.get("state") not in {"prepared", "applied"}
                    or not isinstance(handoff.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", handoff["id"])
                    or handoff.get("point", {}).get("person_id") != self.person_id
                    or handoff["point"].get("gps_accuracy") != 0):
                raise ValueError
            current = coordinates({key: handoff["point"][key] for key in ("latitude", "longitude")})
            current["timestamp"] = timestamp(handoff["native_timestamp"]).isoformat()
            return {**handoff, "current": current}
        except Exception:
            raise GatewayError("invalid handoff point") from None

    async def acknowledge_handoff(self, key, expected_id):
        result = await self._request("/control/handoff/ack", {
            "idempotency_key": key, "source_session": self.session, "expected_id": expected_id})
        status = self._status(result)
        if not status["handoff"] or status["handoff"]["state"] != "idle" or status["handoff"]["id"] == expected_id:
            raise GatewayError("handoff acknowledgment unconfirmed")
        self.ready, self.pending = True, False
        return status

    async def status(self):
        return self._status(await self._request("/control/status"))

    async def close(self, key):
        self.invalidate()
        await self.status()  # Check identity before any mutation.
        result = await self._request("/control/custom", {"idempotency_key": key, "source_session": self.session})
        status = self._status(result)
        if status["upstream_uncertain"] or status["replay_required"] or status["recovery_retryable"]:
            raise GatewayError("gateway recovery pending")
        if result["mode"] != "custom":
            raise GatewayError("gateway close unconfirmed")
        self.pending = False
        self.ready = True

    async def custom(self, key, current):
        self.invalidate()
        await self.status()
        point = {key: current[key] for key in ("latitude", "longitude")}
        point["gps_accuracy"] = 0
        result = await self._request("/control/custom", {
            "idempotency_key": key, "point": point, "source_session": self.session,
        })
        if self._receipt(result.get("receipt"), "custom") is None:
            raise GatewayError("custom point unconfirmed")
        self.pending = False
        self.ready = True

    async def normal(self, key, *, expected_id=_UNSET):
        status = await self.status()
        if not self.ready or self.pending or status["mode"] == "blocked":
            raise GatewayError("gateway recovery pending")
        if status["scheduled_return_supported"] and expected_id is _UNSET:
            raise GatewayError("normal consent missing return generation")
        result = await self._request("/control/normal", {
            "idempotency_key": key, "source_session": self.session,
            **({"expected_id": expected_id} if expected_id is not _UNSET else {}),
        })
        status = self._status(result)
        if status["mode"] not in {"warmup", "normal"}:
            raise GatewayError("normal mode unconfirmed")

    async def configure_return(self, key, expected_id, at):
        if not self.return_supported or not self.ready or self.pending:
            raise GatewayError("scheduled return unavailable")
        result = await self._request("/control/return", {
            "idempotency_key": key, "source_session": self.session,
            "expected_id": expected_id, "at": at,
        })
        status = self._status(result)
        plan = status["scheduled_return"]
        if (plan is None or plan["at"] != at
                or plan["state"] != ("idle" if at is None else "armed")):
            raise GatewayError("scheduled return unconfirmed")
        return status

    async def acknowledge_return(self, key, expected_id):
        result = await self._request("/control/return/ack", {
            "idempotency_key": key, "source_session": self.session, "expected_id": expected_id,
        })
        status = self._status(result)
        plan = status["scheduled_return"]
        if plan is None or plan["state"] != "idle" or plan["id"] == expected_id:
            raise GatewayError("scheduled return acknowledgment unconfirmed")
        return status
