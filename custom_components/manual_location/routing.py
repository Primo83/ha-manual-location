"""Resolve public Maps directions and sample an optional Geoapify route."""

import asyncio
from bisect import bisect_right
import json
import math
from random import randint
import re
import unicodedata
from urllib.parse import parse_qs, unquote, unquote_plus, urljoin, urlsplit

from aiohttp import ClientTimeout

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .coordinator import LocationError, stop_label
from .geocoding import API_KEY_OPTION, valid_api_key

ENDPOINT = "https://api.geoapify.com/v1/routing"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_STOPS = 12
MAX_POINTS = 500
GOOGLE_HOSTS = frozenset({
    "google.com", "www.google.com", "maps.google.com",
    "google.pl", "www.google.pl", "maps.google.pl", "maps.app.goo.gl",
})
MODES = {"walk": "walk", "drive": "drive", "bicycle": "bicycle", "transit": "approximated_transit"}
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


def _number(value, minimum, maximum):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def _coordinates(latitude, longitude):
    if not _number(latitude, -90, 90) or not _number(longitude, -180, 180):
        raise LocationError("invalid_input")
    return {"latitude": float(latitude), "longitude": float(longitude)}


def _url(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 8192
            or "\\" in value or any(unicodedata.category(c).startswith("C") or c.isspace() for c in value)):
        raise LocationError("route_invalid_url")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in GOOGLE_HOSTS
                or parsed.username is not None or parsed.password is not None or parsed.port is not None):
            raise ValueError
        return parsed
    except (ValueError, UnicodeError):
        raise LocationError("route_invalid_url") from None


def _stop(value):
    if not isinstance(value, str):
        raise LocationError("route_invalid_url")
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise LocationError("route_invalid_url")
    value = " ".join(value.split())
    coordinates = re.fullmatch(rf"({NUMBER})\s*,\s*({NUMBER})", value)
    if coordinates:
        try:
            point = _coordinates(*map(float, coordinates.groups()))
        except LocationError:
            raise LocationError("route_invalid_url") from None
        return {**point, "label": value}
    if (not 3 <= len(value) <= 200 or any(unicodedata.category(c).startswith("C") for c in value)
            or value.lower() in {"my location", "your location", "moja lokalizacja", "twoja lokalizacja"}):
        raise LocationError("route_invalid_url")
    return {"query": value}


def parse_google_url(value):
    """Return explicit direction stops; place names still require confirmation."""
    parsed = _url(value)
    if parsed.hostname == "maps.app.goo.gl":
        raise LocationError("route_invalid_url")
    path = unquote(parsed.path)
    if not path.startswith("/maps/dir/") and path != "/maps/dir":
        raise LocationError("route_invalid_url")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        raise LocationError("route_invalid_url") from None
    mode = None
    if "origin" in query or "destination" in query:
        if any(len(query.get(field, [])) != 1 for field in ("origin", "destination")):
            raise LocationError("route_invalid_url")
        if any(len(query.get(field, [])) > 1 for field in ("waypoints", "travelmode")):
            raise LocationError("route_invalid_url")
        values = [query["origin"][0], *query.get("waypoints", [""])[0].split("|"), query["destination"][0]]
        if not query.get("waypoints", [""])[0]:
            values = [query["origin"][0], query["destination"][0]]
        mode = {"walking": "walk", "driving": "drive", "bicycling": "bicycle", "transit": "transit"}.get(
            query.get("travelmode", [None])[0],
        )
    else:
        raw_stops = parsed.path.removeprefix("/maps/dir/").split("/")
        values = []
        for part in raw_stops:
            decoded = unquote_plus(part)
            if decoded.startswith(("@", "data=")):
                break
            if decoded:
                values.append(decoded)
        encoded_modes = set(re.findall(r"!3e([0-3])(?:!|$)", path))
        if len(encoded_modes) == 1:
            mode = {"0": "drive", "1": "bicycle", "2": "walk", "3": "transit"}[encoded_modes.pop()]
    if not 2 <= len(values) <= MAX_STOPS:
        raise LocationError("route_invalid_url")
    stops = [_stop(item) for item in values]
    # Shared links can omit data hints for coordinates already in the path.
    # Remove only exact known matches, then require one hint per named stop.
    data = path.partition("/data=")[2]
    try:
        hints = [_coordinates(float(latitude), float(longitude))
                 for longitude, latitude in re.findall(rf"!1d({NUMBER})!2d({NUMBER})(?=!|$)", data)]
    except LocationError:
        raise LocationError("route_invalid_url") from None
    complete_hints = data.count("!1d") == data.count("!2d") == len(hints)
    for stop in stops:
        if "query" not in stop:
            known = {"latitude": stop["latitude"], "longitude": stop["longitude"]}
            if known in hints:
                hints.remove(known)
    named = [index for index, stop in enumerate(stops) if "query" in stop]
    if complete_hints and len(hints) == len(named):
        for index, coordinates in zip(named, hints, strict=True):
            stops[index] = {**coordinates, "label": stops[index]["query"]}
    return {"mode": mode, "stops": stops}


def _distance(first, second):
    """Short great-circle distance, also across the antimeridian."""
    lon1, lat1, lon2, lat2 = map(math.radians, (*first, *second))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(a)))


def sample_route(payload, interval_seconds, duration_seconds, expected_legs=None, *, interval_max_seconds=None, stop_labels=None):
    """Interpolate within each timed step, retaining slower/faster route parts."""
    try:
        if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" or len(payload["features"]) != 1:
            raise ValueError
        feature = payload["features"][0]
        geometry, properties = feature["geometry"], feature["properties"]
        lines, legs = geometry["coordinates"], properties["legs"]
        distance, provider_time = properties["distance"], properties["time"]
        if (geometry["type"] != "MultiLineString" or not isinstance(lines, list) or not 1 <= len(lines) < MAX_STOPS
                or not isinstance(legs, list) or len(lines) != len(legs)
                or (expected_legs is not None and len(lines) != expected_legs)
                or properties.get("distance_units") != "meters" or not _number(distance, 0, 1000000000)
                or not _number(provider_time, 0.001, 86400 * 31)):
            raise ValueError
        if stop_labels is not None:
            if not isinstance(stop_labels, list) or len(stop_labels) != len(legs) + 1:
                raise ValueError
            stop_labels = [stop_label(label) for label in stop_labels]
        segments = []
        elapsed = 0.0
        stop_times = [0.0]
        previous = None
        vertex_count = 0
        for line, leg in zip(lines, legs, strict=True):
            if not isinstance(line, list) or len(line) < 2:
                raise ValueError
            vertex_count += len(line)
            if vertex_count > 100000:
                raise ValueError
            for point in line:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError
                _coordinates(point[1], point[0])
            if previous is not None and _distance(previous, line[0]) > 10:
                raise ValueError
            steps = leg["steps"]
            if not isinstance(steps, list) or not 1 <= len(steps) <= 100000:
                raise ValueError
            cursor = 0
            for step in steps:
                start, end, seconds = step["from_index"], step["to_index"], step["time"]
                if (type(start) is not int or type(end) is not int or start != cursor or not start <= end < len(line)
                        or not _number(seconds, 0, 86400 * 31)):
                    raise ValueError
                cursor = end
                if start == end:
                    if seconds:
                        segments.append((elapsed, elapsed + seconds, line[start], line[end]))
                        elapsed += seconds
                    continue
                lengths = [_distance(line[index], line[index + 1]) for index in range(start, end)]
                length = sum(lengths)
                if seconds <= 0 or length <= 0:
                    raise ValueError
                for index, size in enumerate(lengths, start):
                    span = seconds * size / length
                    if span:
                        segments.append((elapsed, elapsed + span, line[index], line[index + 1]))
                        elapsed += span
            if cursor != len(line) - 1:
                raise ValueError
            previous = line[-1]
            stop_times.append(elapsed)
        if not segments or elapsed <= 0:
            raise ValueError
        # Minor provider rounding is expected; missing time coverage is not.
        if abs(elapsed - provider_time) > max(5, provider_time * 0.05):
            raise ValueError
        duration = provider_time if duration_seconds is None else duration_seconds
        if not _number(duration, 0.001 if interval_max_seconds is not None else 1, 86400):
            raise LocationError("route_duration_invalid")
        if interval_max_seconds is not None:
            duration = max(1, round(duration))
        sampling_duration = duration
        if interval_max_seconds is None and stop_labels is not None:
            # Date timestamps have millisecond precision; keep the exact end
            # representable before combining samples and mandatory stops.
            duration = math.floor(duration * 1000) / 1000
        offsets = [0]
        while True:
            step = interval_seconds if interval_max_seconds is None else randint(interval_seconds, interval_max_seconds)
            offset = offsets[-1] + step
            if offset >= duration:
                break
            offsets.append(offset)
            # Reserve one slot for the exact destination, even with random steps.
            if len(offsets) >= MAX_POINTS:
                raise LocationError("route_too_many_points")
        offsets.append(duration)
        stops = {}
        if stop_labels is not None:
            positions = [lines[0][0], *(line[-1] for line in lines)]
            for index, (seconds, label, position) in enumerate(zip(stop_times, stop_labels, positions, strict=True)):
                if index == 0:
                    offset = 0
                elif index == len(stop_labels) - 1:
                    offset = duration
                else:
                    offset = sampling_duration * seconds / elapsed
                    offset = round(offset) if interval_max_seconds is not None else math.floor(offset * 1000) / 1000
                if offset in stops:
                    raise ValueError
                stops[offset] = (label, position)
            samples = set(offsets) if interval_max_seconds is not None else {
                duration if offset == duration else math.floor(offset * 1000) / 1000 for offset in offsets
            }
            offsets = sorted(samples | stops.keys())
        if len(offsets) > MAX_POINTS:
            raise LocationError("route_too_many_points")
        ends = [item[1] for item in segments]
        points = []
        for offset in offsets:
            at = elapsed * offset / sampling_duration
            begin, end, first, last = segments[min(bisect_right(ends, at), len(segments) - 1)]
            fraction = min(1, max(0, (at - begin) / (end - begin)))
            longitude_delta = (last[0] - first[0] + 180) % 360 - 180
            longitude = (first[0] + longitude_delta * fraction + 180) % 360 - 180
            point = {"latitude": first[1] + (last[1] - first[1]) * fraction,
                     "longitude": longitude, "offset_seconds": offset}
            if offset in stops:
                label, position = stops[offset]
                point.update(latitude=position[1], longitude=position[0], stop=label)
            points.append(point)
        # Preserve exact provider endpoints despite interpolation/rounding.
        points[0].update(latitude=lines[0][0][1], longitude=lines[0][0][0])
        points[-1].update(latitude=lines[-1][-1][1], longitude=lines[-1][-1][0])
        return points, duration, float(distance)
    except LocationError as error:
        if error.code in {"route_duration_invalid", "route_too_many_points"}:
            raise
        raise LocationError("route_invalid_response") from None
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        raise LocationError("route_invalid_response") from None


def route_cost(stops, distance):
    """Basic request only: waypoint pairs plus distance beyond the first 500 km."""
    # https://www.geoapify.com/pricing-details/ and Routing API examples:
    # 2 waypoints / <=500 km = 1; 2 waypoints / 630 km = 2 units.
    return stops - 1 + max(0, math.ceil(distance / 500000) - 1)


class RouteImporter:
    """Preview-only operations, sharing the geocoder's key, lock and Store."""

    def __init__(self, geocoder):
        self.geocoder = geocoder

    def _check(self, options, authorize):
        self.geocoder._check_options(options)
        if authorize is not None:
            authorize()

    async def async_resolve(self, url, *, authorize=None):
        options = self.geocoder.entry.options
        self._check(options, authorize)
        try:
            async with asyncio.timeout(10):
                for _ in range(3):
                    parsed = _url(url)
                    if parsed.hostname != "maps.app.goo.gl":
                        return parse_google_url(url)
                    self._check(options, authorize)
                    session = async_get_clientsession(self.geocoder.hass)
                    async with session.head(url, allow_redirects=False, timeout=ClientTimeout(total=3)) as response:
                        if response.status not in {301, 302, 303, 307, 308}:
                            raise LocationError("route_invalid_url")
                        location = response.headers.get("Location")
                        if not isinstance(location, str) or not 1 <= len(location) <= 8192:
                            raise LocationError("route_invalid_url")
                        url = urljoin(url, location)
                    self._check(options, authorize)
                return parse_google_url(url)
        except LocationError:
            raise
        except Exception:
            raise LocationError("route_unavailable") from None
        finally:
            self._check(options, authorize)

    async def _request(self, points, mode, key):
        try:
            session = async_get_clientsession(self.geocoder.hass)
            async with session.get(
                ENDPOINT, params={
                    "waypoints": "|".join(f'{point["latitude"]},{point["longitude"]}' for point in points),
                    "mode": mode, "units": "metric", "format": "geojson", "apiKey": key,
                }, timeout=ClientTimeout(total=10), allow_redirects=False,
            ) as response:
                if response.status in {401, 403}:
                    raise LocationError("geocoder_invalid_key")
                if response.status == 429:
                    raise LocationError("geocoder_rate_limited")
                if response.status != 200:
                    raise LocationError("route_unavailable")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise LocationError("route_invalid_response")
                return json.loads(body)
        except LocationError:
            raise
        except Exception:
            raise LocationError("route_unavailable") from None

    async def async_import(self, points, mode, interval_minutes, duration_minutes, *, interval_max_minutes=None, authorize=None):
        options = self.geocoder.entry.options
        self._check(options, authorize)
        if not isinstance(points, list) or not 2 <= len(points) <= MAX_STOPS or not isinstance(mode, str) or mode not in MODES:
            raise LocationError("invalid_input")
        validated, labels = [], []
        for index, point in enumerate(points):
            if not isinstance(point, dict) or set(point) not in ({"latitude", "longitude"}, {"latitude", "longitude", "stop"}):
                raise LocationError("invalid_input")
            validated.append(_coordinates(point["latitude"], point["longitude"]))
            fallback = "Początek" if index == 0 else "Cel" if index == len(points) - 1 else f"Przystanek {index}"
            labels.append(stop_label(point["stop"]) if "stop" in point else fallback)
        if type(interval_minutes) is not int or not 1 <= interval_minutes <= 1440:
            raise LocationError("invalid_input")
        if interval_max_minutes is not None and (type(interval_max_minutes) is not int
                or not interval_minutes <= interval_max_minutes <= 1440):
            raise LocationError("invalid_input")
        if duration_minutes is not None and not _number(duration_minutes, 1 / 60, 1440):
            raise LocationError("route_duration_invalid")
        longest_interval = interval_minutes if interval_max_minutes is None else interval_max_minutes
        if duration_minutes is not None and math.ceil(duration_minutes / longest_interval) + 1 > MAX_POINTS:
            raise LocationError("route_too_many_points")
        key = options.get(API_KEY_OPTION)
        if not key:
            raise LocationError("geocoder_not_configured")
        if not valid_api_key(key):
            raise LocationError("geocoder_invalid_key")
        try:
            # Return a sanitized failure before the client's 12-second deadline.
            async with asyncio.timeout(11):
                day = await self.geocoder.async_reserve_route(options, authorize=authorize)
                self._check(options, authorize)
                payload = await self._request(validated, MODES[mode], key)
                self._check(options, authorize)
                sampled, duration, distance = sample_route(
                    payload, interval_minutes * 60, None if duration_minutes is None else duration_minutes * 60,
                    len(validated) - 1,
                    interval_max_seconds=None if interval_max_minutes is None else interval_max_minutes * 60,
                    stop_labels=labels,
                )
                budget = await self.geocoder.async_settle_route(
                    day, route_cost(len(validated), distance), options, authorize=authorize,
                )
                self._check(options, authorize)
                return {"points": sampled, "mode": mode, "duration_seconds": duration,
                        "distance_meters": distance, **budget}
        except TimeoutError:
            raise LocationError("route_unavailable") from None
        finally:
            self._check(options, authorize)
