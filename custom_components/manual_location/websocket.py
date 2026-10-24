"""WebSocket API with PIN-gated writes and minimal administrator alias discovery."""

import logging

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.messages import BASE_COMMAND_MESSAGE_SCHEMA

from .const import DOMAIN
from .coordinator import LocationError

_LOGGER = logging.getLogger(__name__)
_REFRESH_LOGGER = logging.getLogger(f"{__package__}.coordinator.location_refresh")

FIELDS = {
    "version": set(),
    "view": {"entity_ids", "start", "end"},
    "unlock": {"pin"},
    "admin_get": {"token"},
    "gateway_status": {"token"},
    "gateway_return": {"token", "revision", "expected_id", "local_time"},
    "gateway_return_save": {"token", "revision", "schedule_revision", "event_id", "local_time"},
    "gateway_return_delete": {"token", "revision", "schedule_revision", "event_id"},
    "ha_history": {"token", "entity_id", "start", "end", "cursor"},
    "ha_history_preview": {"token", "entity_id", "revision", "selection"},
    "ha_history_range_preview": {"token", "entity_id", "revision", "start", "end", "include_custom"},
    "ha_history_execute": {"token", "entity_id", "revision", "preview_token", "operation_id"},
    "quick_save_info": set(),
    "refresh_location": {"entity_id"},
    "geocode": {"token", "query"},
    "geocode_reverse": {"token", "latitude", "longitude"},
    "route_resolve": {"token", "url"},
    "route_import": {"token", "points", "mode", "interval_minutes", "duration_minutes"},
    "admin_save": {"token", "revision", "enabled", "persons"},
    "scenario_save": {"token", "revision", "scenario_id", "entity_id", "day", "name", "points"},
    "scenario_action": {"token", "revision", "scenario_id", "action"},
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
            fields.discard("quick_save")
            fields.discard("activity")
            fields.discard("gateway_mode")
            fields.discard("return_schedule")
        if operation in {"scenario_save", "scenario_action"} and "scenario_id" not in fields:
            raise LocationError("client_outdated")
        # Older clients omit the optional upper bound and keep fixed intervals.
        if operation == "route_import":
            fields.discard("interval_max_minutes")
        if operation == "scenario_save":
            fields.discard("routing")
            fields.discard("point_names_version")
            fields.discard("auto_custom")
            fields.discard("auto_ha")
        if operation == "refresh_location":
            fields.discard("wait_for_report")
        if fields != FIELDS[operation]:
            raise LocationError("invalid_input")
        if operation == "version":
            result = {"version": coordinator.version}
        elif operation == "view":
            result = await coordinator.async_view(connection, msg["entity_ids"], msg["start"], msg["end"])
        elif operation == "unlock":
            result = await coordinator.async_unlock(connection, msg["pin"])
        elif operation == "admin_get":
            result = coordinator.admin_get(connection, msg["token"])
        elif operation == "gateway_status":
            result = await coordinator.async_gateway_status(connection, msg["token"])
        elif operation == "gateway_return":
            result = await coordinator.async_gateway_return(
                connection, msg["token"], msg["revision"], msg["expected_id"], msg["local_time"],
            )
        elif operation == "gateway_return_save":
            result = await coordinator.async_gateway_return_save(
                connection, msg["token"], msg["revision"], msg["schedule_revision"], msg["event_id"], msg["local_time"],
            )
        elif operation == "gateway_return_delete":
            result = await coordinator.async_gateway_return_delete(
                connection, msg["token"], msg["revision"], msg["schedule_revision"], msg["event_id"],
            )
        elif operation.startswith("ha_history"):
            coordinator.require_session(connection, msg["token"])
            if coordinator.gateway is None or (operation == "ha_history" and msg["entity_id"] != coordinator.gateway.person_id):
                if operation != "ha_history":
                    raise LocationError("history_unavailable")
                result = {"supported": False, "rows": [], "has_more": False,
                          "next_cursor": None, "revision": coordinator.data["revision"], "history_revision": 0}
            else:
                history = coordinator.location_history
                args = (connection, msg["token"], msg["entity_id"])
                try:
                    if operation == "ha_history":
                        result = await history.async_page(*args, msg["start"], msg["end"], msg["cursor"])
                    elif operation == "ha_history_preview":
                        result = await history.async_preview(*args, msg["revision"], msg["selection"])
                    elif operation == "ha_history_range_preview":
                        result = await history.async_preview(*args, msg["revision"], start=msg["start"],
                                                             end=msg["end"], include_custom=msg["include_custom"])
                    else:
                        result = await history.async_execute(*args, msg["revision"], msg["preview_token"], msg["operation_id"])
                except Exception as error:
                    from .gps_history import GpsHistoryError

                    if isinstance(error, GpsHistoryError):
                        raise LocationError(error.code) from None
                    raise
        elif operation == "quick_save_info":
            result = coordinator.quick_save_info(connection)
        elif operation == "refresh_location":
            result = await coordinator.async_refresh_location(
                connection, msg["entity_id"], wait_for_report=msg.get("wait_for_report", False),
            )
        elif operation in {"geocode", "geocode_reverse"}:
            coordinator.require_session(connection, msg["token"])
            try:
                authorize = lambda: coordinator.require_session(connection, msg["token"])
                if operation == "geocode_reverse":
                    result = await coordinator.geocoder.async_reverse(msg["latitude"], msg["longitude"], authorize=authorize)
                else:
                    result = await coordinator.geocoder.async_search(msg["query"], authorize=authorize)
            finally:
                coordinator.require_session(connection, msg["token"])
        elif operation in {"route_resolve", "route_import"}:
            from .routing import RouteImporter

            coordinator.require_session(connection, msg["token"])
            importer = RouteImporter(coordinator.geocoder)
            authorize = lambda: coordinator.require_session(connection, msg["token"])
            try:
                if operation == "route_resolve":
                    result = await importer.async_resolve(msg["url"], authorize=authorize)
                else:
                    result = await importer.async_import(
                        msg["points"], msg["mode"], msg["interval_minutes"], msg["duration_minutes"],
                        interval_max_minutes=msg.get("interval_max_minutes"), authorize=authorize,
                    )
            finally:
                authorize()
        elif operation == "admin_save":
            result = await coordinator.async_save(
                connection, msg["token"], msg["revision"], msg["enabled"], msg["persons"],
                **({"quick_save": msg["quick_save"]} if "quick_save" in msg else {}),
                **({"activity": msg["activity"]} if "activity" in msg else {}),
                **({"gateway_mode": msg["gateway_mode"]} if "gateway_mode" in msg else {}),
            )
        elif operation == "scenario_save":
            result = await coordinator.async_save_scenario(
                connection, msg["token"], msg["revision"], msg["scenario_id"], msg["entity_id"], msg["day"], msg["name"], msg["points"],
                **({"routing": msg["routing"]} if "routing" in msg else {}),
                **({"point_names_version": msg["point_names_version"]} if "point_names_version" in msg else {}),
                **({"auto_custom": msg["auto_custom"]} if "auto_custom" in msg else {}),
                **({"auto_ha": msg["auto_ha"]} if "auto_ha" in msg else {}),
            )
        elif operation == "scenario_action":
            result = await coordinator.async_scenario_action(
                connection, msg["token"], msg["revision"], msg["scenario_id"], msg["action"],
            )
        elif operation == "change_pin":
            result = await coordinator.async_change_pin(connection, msg["token"], msg["new_pin"])
        else:
            result = coordinator.lock(connection, msg["token"])
        if isinstance(result, dict):
            result = dict(result)
            result.pop("normal_intent", None)
            result.pop("resume_intent_id", None)
            if isinstance(result.get("snapshot"), dict):
                result["snapshot"] = dict(result["snapshot"])
                result["snapshot"].pop("normal_intent", None)
        connection.send_result(msg["id"], result)
    except LocationError as err:
        if operation == "refresh_location" and connection.user is not None and connection.user.is_admin:
            _REFRESH_LOGGER.info("Location refresh rejected: %s", err.code)
        connection.send_error(msg["id"], err.code, err.code)
    except Exception:  # Do not send payloads/exception text to HA's generic logger.
        _LOGGER.error("Manual Location command failed: %s", operation)
        connection.send_error(msg["id"], "unknown_error", "unknown_error")
