"""Include a consistent SQLite history snapshot in Home Assistant backups."""

from .const import DOMAIN


async def async_pre_backup(hass):
    coordinator = hass.data.get(DOMAIN)
    history = coordinator._location_history if coordinator is not None else None
    if history is not None and history.db is not None:
        await history.db.async_backup(hass.config.path(".storage", "manual_location_gps.backup.sqlite"))


async def async_post_backup(hass):
    """No writer is frozen: SQLite's online backup created a stable snapshot."""
