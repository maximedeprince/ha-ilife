"""Config flow for ILIFE: email + password (+ region), with reauth."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_REGION

from .api import REGIONS, ILifeAccount, ILifeAuthError, ILifeError
from .brands import BRANDS, DEFAULT_BRAND
from .const import CONF_BRAND, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def _validate(hass, email_addr: str, password: str, region: str, brand: str) -> None:
    """Raise ILifeAuthError / ILifeError if credentials are wrong / unreachable."""
    account = ILifeAccount(email_addr, password, region, brand)
    await hass.async_add_executor_job(account.login)


class ILifeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._reauth_email: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email_addr = user_input[CONF_EMAIL]
            try:
                await _validate(self.hass, email_addr, user_input[CONF_PASSWORD],
                                user_input[CONF_REGION], user_input[CONF_BRAND])
            except ILifeAuthError:
                errors["base"] = "invalid_auth"
            except ILifeError as err:
                _LOGGER.debug("ILIFE cannot_connect: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected ILIFE login error")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(email_addr.lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=email_addr, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_BRAND, default=DEFAULT_BRAND):
                    vol.In({k: b.name for k, b in BRANDS.items()}),
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_REGION, default="eu"): vol.In(list(REGIONS)),
            }),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._reauth_email = entry_data[CONF_EMAIL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            region = entry.data.get(CONF_REGION, "eu")
            brand = entry.data.get(CONF_BRAND, DEFAULT_BRAND)
            try:
                await _validate(self.hass, entry.data[CONF_EMAIL],
                                user_input[CONF_PASSWORD], region, brand)
            except ILifeAuthError:
                errors["base"] = "invalid_auth"
            except ILifeError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]})

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": self._reauth_email or ""},
            errors=errors,
        )
