"""WebSocket API; view and public version need no administrator PIN session."""

import logging

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.messages import BASE_COMMAND_MESSAGE_SCHEMA

from .const import DOMAIN
from .coordinator import LocationError

_LOGGER = logging.getLogger(__name__)

FIELDS = {
    "version": set(),
    "view": {"entity_ids", "start", "end"},
    "unlock": {"pin"},
    "admin_get": {"token"},
    "admin_save": {"token", "revision", "enabled", "persons"},
    "scenario_save": {"token", "revision", "entity_id", "name", "points"},
    "scenario_action": {"token", "revision", "entity_id", "action"},
    "lock": {"token"},
    "change_pin": {"token", "new_pin"},
}


def async_register_commands(hass):
    for operation in FIELDS:
        command = f"{DOMAIN}/{operation}"
        schema = BASE_COMMAND_MESSAGE_SCHEMA.extend({"type": command}, extra=vol.ALLOW_EXTRA)
        websocket_api.async_register_command(hass, command, async_handle_command, schema)


@websocket_api.async_response
async def async_handle_command(hass, connection, msg):
    operation = msg["type"].split("/", 1)[1]
    try:
        if connection.user is None or (operation not in {"view", "version"} and not connection.user.is_admin):
            raise LocationError("unauthorized")
        if (coordinator := hass.data.get(DOMAIN)) is None:
            raise LocationError("not_loaded")
        fields = set(msg) - {"id", "type"}
        # 0.1.9 sends its entire admin snapshot. Its echoed scenarios are
        # read-only and must never overwrite the server's authoritative plans.
        if operation == "admin_save":
            fields.discard("scenarios")
        if fields != FIELDS[operation]:
            raise LocationError("invalid_input")
        if operation == "version":
            result = {"version": coordinator.version}
        elif operation == "view":
            result = coordinator.view(connection, msg["entity_ids"], msg["start"], msg["end"])
        elif operation == "unlock":
            result = await coordinator.async_unlock(connection, msg["pin"])
        elif operation == "admin_get":
            result = coordinator.admin_get(connection, msg["token"])
        elif operation == "admin_save":
            result = await coordinator.async_save(connection, msg["token"], msg["revision"], msg["enabled"], msg["persons"])
        elif operation == "scenario_save":
            result = await coordinator.async_save_scenario(
                connection, msg["token"], msg["revision"], msg["entity_id"], msg["name"], msg["points"],
            )
        elif operation == "scenario_action":
            result = await coordinator.async_scenario_action(
                connection, msg["token"], msg["revision"], msg["entity_id"], msg["action"],
            )
        elif operation == "change_pin":
            result = await coordinator.async_change_pin(connection, msg["token"], msg["new_pin"])
        else:
            result = coordinator.lock(connection, msg["token"])
        connection.send_result(msg["id"], result)
    except LocationError as err:
        connection.send_error(msg["id"], err.code, err.code)
    except Exception:  # Do not send payloads/exception text to HA's generic logger.
        _LOGGER.error("Manual Location command failed: %s", operation)
        connection.send_error(msg["id"], "unknown_error", "unknown_error")
