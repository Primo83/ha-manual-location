"""Optional loopback control client; no phone data or credentials reach the panel."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from urllib.parse import urlsplit

from aiohttp import ClientTimeout

from homeassistant.core import valid_entity_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession


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
        blocked = self.pending or result.get("upstream_uncertain") or result.get("replay_required") or result.get("recovery_retryable")
        return {"enabled": True, "available": True, "person_id": self.person_id,
                "mode": "blocked" if blocked else result["mode"],
                "gps_blocked": result["mode"] in {"custom", "warmup"} and not bool(result.get("upstream_uncertain")),
                "remaining_seconds": max(0, math.ceil(remaining)), "pending": self.pending,
                "replay_required": bool(result.get("replay_required")),
                "upstream_uncertain": bool(result.get("upstream_uncertain")),
                "recovery_retryable": bool(result.get("recovery_retryable")),
                "has_custom_point": custom is not None,
                "last_custom_receipt": custom, "last_phone_receipt": phone}

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

    async def normal(self, key):
        status = await self.status()
        if not self.ready or self.pending or status["mode"] == "blocked":
            raise GatewayError("gateway recovery pending")
        result = await self._request("/control/normal", {
            "idempotency_key": key, "source_session": self.session,
        })
        status = self._status(result)
        if status["mode"] not in {"warmup", "normal"}:
            raise GatewayError("normal mode unconfirmed")
