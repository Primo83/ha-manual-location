"""Configure the single Manual Location instance."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import DOMAIN
from .geocoding import API_KEY_OPTION, valid_api_key
from .security import hash_pin, valid_pin


class ManualLocationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set a local PIN without a default value."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ManualLocationOptionsFlow()

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


class ManualLocationOptionsFlow(OptionsFlow):
    """Keep the optional provider key in HA options, never in form defaults."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            key = user_input.get(API_KEY_OPTION, "")
            remove = user_input.get("remove_api_key", False)
            if not isinstance(key, str) or (key and not valid_api_key(key)):
                errors[API_KEY_OPTION] = "invalid_api_key"
            elif type(remove) is not bool:
                errors["base"] = "invalid_input"
            else:
                options = dict(self.config_entry.options)
                if remove:
                    options.pop(API_KEY_OPTION, None)
                elif key:
                    options[API_KEY_OPTION] = key
                return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(API_KEY_OPTION): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="one-time-code")
                ),
                vol.Optional("remove_api_key", default=False): bool,
            }),
            errors=errors,
        )
