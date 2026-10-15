"""Configure the single Manual Location instance."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import DOMAIN
from .security import hash_pin, valid_pin


class ManualLocationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set a local PIN without a default value."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            if not valid_pin(user_input.get("pin")):
                errors["pin"] = "invalid_pin"
            else:
                pin_hash = await self.hass.async_add_executor_job(hash_pin, user_input["pin"])
                return self.async_create_entry(title="Manual Location", data={"pin_hash": pin_hash})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("pin"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }),
            errors=errors,
        )
