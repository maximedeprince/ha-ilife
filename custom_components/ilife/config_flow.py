"""Config flow for ILIFE: choose a backend (ILIFEHOME or ILIFE Clean), then sign in."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_REGION

from .api import REGIONS, ILifeAccount, ILifeAuthError, ILifeError
from .brands import BRANDS, DEFAULT_BRAND
from .const import (
    BACKEND_ILIFE_CLEAN,
    BACKEND_ILIFEHOME,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_BACKEND,
    CONF_BRAND,
    CONF_UID,
    DOMAIN,
)
from .tuya_api import DEFAULT_TUYA_REGION, TUYA_REGIONS, TuyaAuthError, TuyaClient, TuyaError

_LOGGER = logging.getLogger(__name__)


async def _validate(hass, email_addr: str, password: str, region: str, brand: str) -> None:
    """Raise ILifeAuthError / ILifeError if credentials are wrong / unreachable."""
    account = ILifeAccount(email_addr, password, region, brand)
    await hass.async_add_executor_job(account.login)


async def _validate_ilife_clean(hass, access_id: str, access_secret: str, uid: str,
                                region: str) -> None:
    """Raise TuyaAuthError / TuyaError if the Cloud Project credentials/UID are wrong."""
    client = TuyaClient(access_id, access_secret, uid, region)
    await hass.async_add_executor_job(client.list_devices)


class ILifeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._reauth_email: str | None = None

    # --- backend picker ---
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="user",
            menu_options=["ilifehome", "ilife_clean"],
        )

    # --- ILIFEHOME (Alibaba IoT cloud) ---
    async def async_step_ilifehome(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email_addr = user_input[CONF_EMAIL]
            try:
                await _validate(self.hass, email_addr, user_input[CONF_PASSWORD],
                                user_input[CONF_REGION], user_input[CONF_BRAND])
            except ILifeAuthError as err:
                _LOGGER.warning("ILIFE authentication failed: %s", err)
                errors["base"] = "invalid_auth"
            except ILifeError as err:
                _LOGGER.debug("ILIFEHOME cannot_connect: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected ILIFEHOME login error")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{BACKEND_ILIFEHOME}_{email_addr.lower()}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=email_addr,
                    data={**user_input, CONF_BACKEND: BACKEND_ILIFEHOME},
                )

        return self.async_show_form(
            step_id="ilifehome",
            data_schema=vol.Schema({
                vol.Required(CONF_BRAND, default=DEFAULT_BRAND):
                    vol.In({k: b.name for k, b in BRANDS.items()}),
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_REGION, default="eu"): vol.In(list(REGIONS)),
            }),
            errors=errors,
        )

    # --- ILIFE Clean (Tuya Cloud Project) ---
    async def async_step_ilife_clean(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _validate_ilife_clean(
                    self.hass, user_input[CONF_ACCESS_ID], user_input[CONF_ACCESS_SECRET],
                    user_input[CONF_UID], user_input[CONF_REGION])
            except TuyaAuthError:
                errors["base"] = "invalid_auth"
            except TuyaError as err:
                _LOGGER.debug("ILIFE Clean cannot_connect: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected ILIFE Clean login error")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{BACKEND_ILIFE_CLEAN}_{user_input[CONF_UID]}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="ILIFE Clean",
                    data={**user_input, CONF_BACKEND: BACKEND_ILIFE_CLEAN},
                )

        return self.async_show_form(
            step_id="ilife_clean",
            data_schema=vol.Schema({
                vol.Required(CONF_ACCESS_ID): str,
                vol.Required(CONF_ACCESS_SECRET): str,
                vol.Required(CONF_UID): str,
                vol.Required(CONF_REGION, default=DEFAULT_TUYA_REGION): vol.In(list(TUYA_REGIONS)),
            }),
            errors=errors,
        )

    # --- reauth ---
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        if entry_data.get(CONF_BACKEND) == BACKEND_ILIFE_CLEAN:
            return await self.async_step_reauth_confirm_tuya()
        self._reauth_email = entry_data.get(CONF_EMAIL)
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
            except ILifeAuthError as err:
                _LOGGER.warning("ILIFE re-authentication failed: %s", err)
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

    async def async_step_reauth_confirm_tuya(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            region = entry.data.get(CONF_REGION, DEFAULT_TUYA_REGION)
            try:
                await _validate_ilife_clean(
                    self.hass, user_input[CONF_ACCESS_ID], user_input[CONF_ACCESS_SECRET],
                    entry.data[CONF_UID], region)
            except TuyaAuthError:
                errors["base"] = "invalid_auth"
            except TuyaError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **user_input})

        return self.async_show_form(
            step_id="reauth_confirm_tuya",
            data_schema=vol.Schema({
                vol.Required(CONF_ACCESS_ID, default=entry.data.get(CONF_ACCESS_ID, "")): str,
                vol.Required(CONF_ACCESS_SECRET): str,
            }),
            errors=errors,
        )
