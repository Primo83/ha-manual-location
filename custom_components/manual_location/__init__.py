"""A private presentation layer for manually selected person positions."""

from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PANEL_PATH, STATIC_PATH
from .coordinator import LocationError, ManualLocation
from .websocket import async_register_commands

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


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
    if not hass.data.get(f"{DOMAIN}_static_registered"):
        await hass.http.async_register_static_paths([
            StaticPathConfig(STATIC_PATH, str(Path(__file__).parent / "frontend"), False)
        ])
        hass.data[f"{DOMAIN}_static_registered"] = True
    await async_register_panel(
        hass, frontend_url_path=PANEL_PATH, webcomponent_name="manual-location-admin",
        sidebar_title="Ustawienia mapy", sidebar_icon="mdi:map-marker-radius",
        module_url=f"{STATIC_PATH}/admin.js", require_admin=True,
        config_panel_domain=DOMAIN,
    )
    hass.data[DOMAIN] = coordinator
    return True


async def async_unload_entry(hass, entry):
    if (coordinator := hass.data.pop(DOMAIN, None)) is not None:
        coordinator.loaded = False
        coordinator.sessions.clear()
        frontend.async_remove_panel(hass, PANEL_PATH)
    return True


async def async_remove_entry(hass, entry):
    """Delete this integration's manual data when its configuration is removed."""
    await ManualLocation(hass, entry).store.async_remove()
