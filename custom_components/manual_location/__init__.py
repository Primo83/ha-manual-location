"""A private presentation layer for manually selected person positions."""

from pathlib import Path

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
        return web.json_response({"session": bridge.session})


async def async_setup(hass, config):
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
