"""Optional Geoapify search with a durable shared daily budget."""

import asyncio
from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, date, datetime
import json
import math
import re
import time
import unicodedata

from aiohttp import ClientTimeout

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import LocationError

API_KEY_OPTION = "geoapify_api_key"
USAGE_STORAGE_KEY = f"{DOMAIN}_geocoding"
USAGE_INITIALIZED = "geocoding_storage_initialized"
ENDPOINT = "https://api.geoapify.com/v1/geocode/search"
DAILY_LIMIT = 2500
MAX_BODY_BYTES = 256 * 1024
CACHE_SECONDS = 3600
RESULT_TYPES = frozenset({
    "unknown", "amenity", "building", "street", "suburb", "district", "postcode",
    "city", "county", "state", "country",
})


def valid_api_key(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) is not None


def normalize_query(value):
    if not isinstance(value, str) or len(value) > 1000 or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise LocationError("invalid_input")
    normalized = " ".join(value.split())
    if not 3 <= len(normalized) <= 200:
        raise LocationError("invalid_input")
    return normalized


def _text(value, maximum, key):
    if not isinstance(value, str) or key in value:
        return ""
    # Display strings remain plain text; the frontend also uses textContent.
    value = re.sub(r"<[^>]*>", "", value[:maximum * 4])
    value = "".join(character for character in value if not unicodedata.category(character).startswith("C"))
    return " ".join(value.split())[:maximum]


def _results(payload, key):
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise LocationError("geocoder_unavailable")
    results = []
    for item in payload["results"][:5]:
        if not isinstance(item, dict):
            continue
        latitude, longitude = item.get("lat"), item.get("lon")
        if any(
            type(value) not in (int, float) or not -bound <= value <= bound or not math.isfinite(value)
            for value, bound in ((latitude, 90), (longitude, 180))
        ):
            continue
        label = _text(item.get("formatted", item.get("name")), 300, key)
        if not label:
            continue
        datasource = item.get("datasource")
        source = datasource.get("attribution", "") if isinstance(datasource, dict) else ""
        result_type = item.get("result_type")
        results.append({
            "latitude": float(latitude), "longitude": float(longitude), "label": label,
            "result_type": result_type if isinstance(result_type, str) and result_type in RESULT_TYPES else "unknown",
            "housenumber": _text(item.get("housenumber", ""), 40, key),
            "attribution": _text(source, 200, key),
        })
    return results


class PlaceGeocoder:
    """Use HA's HTTP session; failures affect search alone, never map storage."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.store = Store(hass, 1, USAGE_STORAGE_KEY, private=True, atomic_writes=True, serialize_in_event_loop=False)
        self._lock = asyncio.Lock()
        self._usage = None
        self._storage_failed = False
        self._last_request = -math.inf
        self._options = entry.options
        self._cache = OrderedDict()

    def _check_options(self, options):
        if self.entry.options is not self._options:
            self._options = self.entry.options
            self._cache.clear()
        if self.entry.options is not options:
            raise LocationError("geocoder_unavailable")

    async def _load_usage(self):
        if self._storage_failed:
            raise LocationError("geocoder_storage_error")
        if self._usage is not None:
            return
        try:
            usage = await self.store.async_load()
            if usage is None:
                if self.entry.data.get(USAGE_INITIALIZED):
                    raise ValueError
                usage = {"day": datetime.now(UTC).date().isoformat(), "count": 0}
            if (
                not isinstance(usage, dict) or set(usage) != {"day", "count"}
                or not isinstance(usage["day"], str) or date.fromisoformat(usage["day"]).isoformat() != usage["day"]
                or type(usage["count"]) is not int or not 0 <= usage["count"] <= DAILY_LIMIT
            ):
                raise ValueError
            self._usage = usage
        except Exception:
            self._storage_failed = True
            raise LocationError("geocoder_storage_error") from None

    def _remaining(self, day):
        return DAILY_LIMIT - (self._usage["count"] if self._usage["day"] == day else 0)

    async def _reserve(self, day, units=1):
        count = DAILY_LIMIT - self._remaining(day) + units
        usage = {"day": day, "count": count}
        try:
            await self.store.async_save(usage)
            if await self.store.async_load() != usage:
                raise ValueError
            self._usage = usage
            if not self.entry.data.get(USAGE_INITIALIZED):
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, USAGE_INITIALIZED: True},
                )
        except asyncio.CancelledError:
            self._storage_failed = True
            raise
        except Exception:
            self._storage_failed = True
            raise LocationError("geocoder_storage_error") from None

    async def async_reserve_route(self, options, *, authorize=None):
        """Persist the conservative routing reservation in the search budget."""
        async with self._lock:
            self._check_options(options)
            await self._load_usage()
            self._check_options(options)
            if authorize is not None:
                authorize()
            day = datetime.now(UTC).date().isoformat()
            if self._remaining(day) < 100:
                raise LocationError("geocoder_daily_limit")
            if time.monotonic() - self._last_request < 1:
                raise LocationError("geocoder_rate_limited")
            await self._reserve(day, 100)
            self._check_options(options)
            if authorize is not None:
                authorize()
            self._last_request = time.monotonic()
            return day

    async def async_settle_route(self, day, cost, options, *, authorize=None):
        """Release confirmed excess units, without refunding a newer UTC day."""
        async with self._lock:
            self._check_options(options)
            await self._load_usage()
            self._check_options(options)
            if authorize is not None:
                authorize()
            adjustment = cost - 100
            # A request reserved yesterday must never refund today's searches.
            if self._usage["day"] != day:
                day = datetime.now(UTC).date().isoformat()
                adjustment = max(0, adjustment)
            remaining = self._remaining(day)
            exhausted = adjustment > remaining
            if adjustment:
                await self._reserve(day, min(adjustment, remaining))
                self._check_options(options)
                if authorize is not None:
                    authorize()
            if exhausted:
                raise LocationError("geocoder_daily_limit")
            today = datetime.now(UTC).date().isoformat()
            return {"remaining": self._remaining(today), "limit": DAILY_LIMIT, "day": today}

    async def _request(self, query, key):
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                ENDPOINT,
                params={"text": query, "lang": "pl", "limit": 5, "format": "json", "bias": "countrycode:pl", "apiKey": key},
                timeout=ClientTimeout(total=12), allow_redirects=False,
            ) as response:
                if response.status in {401, 403}:
                    raise LocationError("geocoder_invalid_key")
                if response.status == 429:
                    raise LocationError("geocoder_rate_limited")
                if response.status != 200:
                    raise LocationError("geocoder_unavailable")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise LocationError("geocoder_unavailable")
                return _results(json.loads(body), key)
        except LocationError:
            raise
        except Exception:
            # URLs and exception strings can contain both the key and query.
            raise LocationError("geocoder_unavailable") from None

    async def async_search(self, query, *, authorize=None):
        if authorize is not None:
            authorize()
        query = normalize_query(query)
        options = self.entry.options
        self._check_options(options)
        key = options.get(API_KEY_OPTION)
        if not key:
            raise LocationError("geocoder_not_configured")
        if not valid_api_key(key):
            raise LocationError("geocoder_invalid_key")
        async with self._lock:
            self._check_options(options)
            await self._load_usage()
            self._check_options(options)
            if authorize is not None:
                authorize()
            day = datetime.now(UTC).date().isoformat()
            now = time.monotonic()
            cached = self._cache.get(query)
            if cached is not None and now - cached[0] < CACHE_SECONDS:
                self._cache.move_to_end(query)
                return {"results": deepcopy(cached[1]), "remaining": self._remaining(day), "limit": DAILY_LIMIT, "day": day}
            if self._remaining(day) == 0:
                raise LocationError("geocoder_daily_limit")
            if now - self._last_request < 1:
                raise LocationError("geocoder_rate_limited")
            await self._reserve(day)
            self._check_options(options)
            if authorize is not None:
                authorize()
            self._last_request = time.monotonic()
        try:
            results = await self._request(query, key)
        finally:
            self._check_options(options)
        self._cache[query] = (time.monotonic(), deepcopy(results))
        self._cache.move_to_end(query)
        while len(self._cache) > 50:
            self._cache.popitem(last=False)
        day = datetime.now(UTC).date().isoformat()
        return {"results": results, "remaining": self._remaining(day), "limit": DAILY_LIMIT, "day": day}
