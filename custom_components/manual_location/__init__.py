"""A private presentation layer for manually selected person positions."""

from pathlib import Path
import hmac
import secrets

from aiohttp import web

from homeassistant.components import frontend
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .const import DOMAIN, PANEL_PATH, STATIC_PATH
from .coordinator import LocationError, ManualLocation
from .gateway_bridge import GatewayError
from .geocoding import PlaceGeocoder
from .phone_activity import CACHE_KEY
from .websocket import async_register_commands

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


class FrontendView(HomeAssistantView):
    """Serve only packaged frontend files, without retaining old clients in cache."""

    url = f"{STATIC_PATH}/{{filename:.+}}"
    name = f"{DOMAIN}:frontend"
    requires_auth = False
    files = frozenset({
        "admin.js", "data.js", "map-common.js", "map.js", "updates.js", "scenarios.js", "place-search.js", "route-import.js",
        "vendor/leaflet.js", "vendor/leaflet.css",
        "vendor/images/layers-2x.png", "vendor/images/layers.png",
        "vendor/images/marker-icon-2x.png", "vendor/images/marker-icon.png",
        "vendor/images/marker-shadow.png",
    })

    async def get(self, request, filename):
        if filename not in self.files:
            raise web.HTTPNotFound()
        return web.FileResponse(
            Path(__file__).parent / "frontend" / filename,
            headers={"Cache-Control": "no-store"},
        )


class GatewaySessionView(HomeAssistantView):
    """Expose only the live HA instance epoch to its authenticated gateway."""

    url = "/api/manual_location/gateway_session"
    name = f"{DOMAIN}:gateway_session"
    requires_auth = True

    async def get(self, request):
        if not request["hass_user"].is_admin:
            raise web.HTTPForbidden()
        coordinator = request.app[KEY_HASS].data.get(DOMAIN)
        bridge = coordinator.gateway if coordinator is not None else None
        if bridge is None or not coordinator.loaded or (not bridge.ready and request.query.get("control") != "1"):
            raise web.HTTPServiceUnavailable()
        if request.query.get("control") == "1":
            try:
                return web.json_response(await coordinator.async_gateway_control_state())
            except LocationError:
                raise web.HTTPServiceUnavailable() from None
        if not await coordinator.location_history.async_ready():
            raise web.HTTPServiceUnavailable()
        return web.json_response({"session": bridge.session,
                                  "core_boot": request.app[KEY_HASS].data.setdefault(f"{DOMAIN}_core_boot", secrets.token_urlsafe(32))})


class GatewayHistoryView(HomeAssistantView):
    """Accept an approved native event from the authenticated private gateway."""

    url = "/api/manual_location/gateway_history"
    name = f"{DOMAIN}:gateway_history"
    requires_auth = True

    async def post(self, request):
        coordinator = request.app[KEY_HASS].data.get(DOMAIN)
        bridge = coordinator.gateway if coordinator is not None else None
        if not request["hass_user"].is_admin or bridge is None or not coordinator.loaded:
            raise web.HTTPForbidden()
        raw = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            raw.extend(chunk)
            if len(raw) > 16384:
                raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=len(raw))
        try:
            import json

            payload = json.loads(raw)
        except Exception:
            return web.json_response({"error": "history unavailable"}, status=409)
        supplied = payload.pop("gateway_key", "") if isinstance(payload, dict) else ""
        # Supervisor deliberately strips custom request headers. Keep the extra
        # private credential in the POST body, in addition to native HA auth.
        supplied = supplied or request.headers.get("X-Manual-Location-Key", "")
        if (not isinstance(supplied, str) or len(supplied) > 512 or not supplied.isascii()
                or not hmac.compare_digest(supplied, bridge._token)):
            raise web.HTTPForbidden()
        try:
            result = await coordinator.location_history.async_receive(payload)
        except Exception:
            # Never log callback data, credentials or exception payloads.
            return web.json_response({"error": "history unavailable"}, status=409)
        return web.json_response(result)


class GatewayHistoryQueryView(GatewayHistoryView):
    """Read only an exactly bound already accepted operation after a lost ACK."""

    url = "/api/manual_location/gateway_history/query"
    name = f"{DOMAIN}:gateway_history_query"

    async def post(self, request):
        coordinator = request.app[KEY_HASS].data.get(DOMAIN)
        bridge = coordinator.gateway if coordinator is not None else None
        if not request["hass_user"].is_admin or bridge is None or not coordinator.loaded:
            raise web.HTTPForbidden()
        raw = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            raw.extend(chunk)
            if len(raw) > 16384:
                raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=len(raw))
        try:
            import json
            payload = json.loads(raw)
        except Exception:
            return web.json_response({"error": "history unavailable"}, status=409)
        key = payload.pop("gateway_key", "") if isinstance(payload, dict) else ""
        if not isinstance(key, str) or len(key) > 512 or not key.isascii() or not hmac.compare_digest(key, bridge._token):
            raise web.HTTPForbidden()
        try:
            result = await coordinator.location_history.async_query(payload)
        except Exception:
            return web.json_response({"error": "history unavailable"}, status=409)
        return web.json_response(result)


class GatewayHistoryCorrectionView(GatewayHistoryView):
    """Execute an exact authorized deletion under the Gateway writer barrier."""

    url = "/api/manual_location/gateway_history/correct"
    name = f"{DOMAIN}:gateway_history_correction"

    async def post(self, request):
        coordinator = request.app[KEY_HASS].data.get(DOMAIN)
        bridge = coordinator.gateway if coordinator is not None else None
        if not request["hass_user"].is_admin or bridge is None or not coordinator.loaded:
            raise web.HTTPForbidden()
        raw = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            raw.extend(chunk)
            if len(raw) > 16384:
                raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=len(raw))
        try:
            import json
            payload = json.loads(raw)
        except Exception:
            return web.json_response({"error": "history unavailable"}, status=409)
        key = payload.pop("gateway_key", "") if isinstance(payload, dict) else ""
        if not isinstance(key, str) or len(key) > 512 or not key.isascii() or not hmac.compare_digest(key, bridge._token):
            raise web.HTTPForbidden()
        try:
            result = await coordinator.location_history.async_correction(payload)
        except Exception:
            return web.json_response({"error": "history unavailable"}, status=409)
        return web.json_response(result)


async def async_setup(hass, config):
    hass.data.setdefault(f"{DOMAIN}_core_boot", secrets.token_urlsafe(32))
    async_register_commands(hass)
    return True


async def async_setup_entry(hass, entry):
    if DOMAIN in hass.data:
        return False
    try:
        coordinator = ManualLocation(hass, entry)
        await coordinator.async_load()
    except (LocationError, GatewayError) as err:
        raise ConfigEntryNotReady("Manual Location storage or gateway configuration is unavailable") from err
    integration = await async_get_integration(hass, DOMAIN)
    coordinator.version = str(integration.version)
    gateway_settings = entry.options.get("gateway")

    async def gateway_options_changed(hass, updated_entry):
        # Provider-key edits retain 1.x behaviour. A changed adapter gets a new
        # RAM epoch and starts closed through the normal unload/setup path.
        if updated_entry.options.get("gateway") != gateway_settings:
            await hass.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(gateway_options_changed))
    if not hass.data.get(f"{DOMAIN}_static_registered"):
        hass.http.register_view(FrontendView)
        hass.http.register_view(GatewaySessionView)
        hass.http.register_view(GatewayHistoryView)
        hass.http.register_view(GatewayHistoryQueryView)
        hass.http.register_view(GatewayHistoryCorrectionView)
        hass.data[f"{DOMAIN}_static_registered"] = True
    await async_register_panel(
        hass, frontend_url_path=PANEL_PATH, webcomponent_name="manual-location-admin",
        sidebar_title="Ustawienia mapy", sidebar_icon="mdi:map-marker-radius",
        module_url=f"{STATIC_PATH}/admin.js?v={coordinator.version}", require_admin=True,
    )
    hass.data[DOMAIN] = coordinator
    await coordinator.async_start()
    return True


async def async_unload_entry(hass, entry):
    if (coordinator := hass.data.pop(DOMAIN, None)) is not None:
        await coordinator.async_shutdown()
        frontend.async_remove_panel(hass, PANEL_PATH)
    return True


async def async_remove_entry(hass, entry):
    """Delete this integration's manual data when its configuration is removed."""
    await ManualLocation(hass, entry).store.async_remove()
    await PlaceGeocoder(hass, entry).store.async_remove()
    await Store(hass, 1, CACHE_KEY, private=True, atomic_writes=True).async_remove()
    from .gps_history import GpsHistory

    await GpsHistory.async_remove_files(hass)
