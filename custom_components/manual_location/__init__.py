"""A private presentation layer for manually selected person positions."""

from pathlib import Path

from aiohttp import web

from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.loader import async_get_integration

from .const import DOMAIN, PANEL_PATH, STATIC_PATH
from .coordinator import LocationError, ManualLocation
from .geocoding import PlaceGeocoder
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


async def async_setup(hass, config):
    async_register_commands(hass)
    return True


async def async_setup_entry(hass, entry):
    if DOMAIN in hass.data:
        return False
    coordinator = ManualLocation(hass, entry)
    try:
        await coordinator.async_load()
    except LocationError as err:
        raise ConfigEntryNotReady("Manual Location storage is unavailable") from err
    integration = await async_get_integration(hass, DOMAIN)
    coordinator.version = str(integration.version)
    if not hass.data.get(f"{DOMAIN}_static_registered"):
        hass.http.register_view(FrontendView)
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
