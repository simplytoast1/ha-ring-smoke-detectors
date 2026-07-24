"""Config flow for Ring Smoke Detectors.

Handles the Ring authentication flow with 2FA support:
1. User enters email + password
2. If 2FA is required, user enters verification code
3. Refresh token is stored in config entry

Reauthentication reuses the same login logic but updates the existing
entry in place instead of creating a new one.
"""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CONF_REFRESH_TOKEN
from .ring_api.auth import (
    Ring2FARequired,
    RingApiError,
    RingAuthError,
    RingRestClient,
)

_LOGGER = logging.getLogger(__name__)


class RingSmokeDetectorsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Ring Smoke Detectors integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._rest_client: RingRestClient | None = None
        self._email: str = ""
        self._password: str = ""
        self._2fa_prompt: str = ""
        self._reauth_entry: ConfigEntry | None = None

    async def _async_attempt_login(
        self, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Try to log in with the stored email and password.

        Returns a flow result on success or when 2FA is needed;
        records an error key and returns None otherwise.
        """
        self._rest_client = RingRestClient(
            session=async_get_clientsession(self.hass),
            email=self._email,
            password=self._password,
        )

        try:
            refresh_token = await self._rest_client.authenticate()
        except Ring2FARequired:
            self._2fa_prompt = self._rest_client.prompt_for_2fa or (
                "Enter verification code"
            )
            return await self.async_step_2fa()
        except RingAuthError as err:
            _LOGGER.error("Ring authentication failed: %s", err)
            errors["base"] = "invalid_auth"
        except RingApiError as err:
            _LOGGER.error("Could not reach Ring: %s", err)
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during Ring login")
            errors["base"] = "unknown"
        else:
            return await self._async_finish(refresh_token)

        return None

    async def _async_finish(self, refresh_token: str) -> ConfigFlowResult:
        """Create the config entry, or update it during reauth."""
        await self.async_set_unique_id(self._email.lower())

        if self._reauth_entry is not None:
            if (
                self._reauth_entry.unique_id
                and self._reauth_entry.unique_id != self._email.lower()
            ):
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data={
                    **self._reauth_entry.data,
                    CONF_REFRESH_TOKEN: refresh_token,
                },
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Ring ({self._email})",
            data={CONF_REFRESH_TOKEN: refresh_token},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: email + password login."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input["email"]
            self._password = user_input["password"]

            result = await self._async_attempt_login(errors)
            if result is not None:
                return result

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("email"): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the 2FA verification step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            assert self._rest_client is not None

            try:
                refresh_token = await self._rest_client.authenticate(
                    two_factor_code=user_input["code"]
                )
            except Ring2FARequired:
                self._2fa_prompt = self._rest_client.prompt_for_2fa or (
                    "Invalid code. Please try again."
                )
                errors["base"] = "invalid_2fa_code"
            except RingAuthError as err:
                _LOGGER.error("Ring 2FA failed: %s", err)
                errors["base"] = "invalid_auth"
            except RingApiError as err:
                _LOGGER.error("Could not reach Ring: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during 2FA")
                errors["base"] = "unknown"
            else:
                return await self._async_finish(refresh_token)

        return self.async_show_form(
            step_id="2fa",
            data_schema=vol.Schema(
                {
                    vol.Required("code"): str,
                }
            ),
            description_placeholders={"tfa_prompt": self._2fa_prompt},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when the token expires."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for credentials again and update the existing entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input["email"]
            self._password = user_input["password"]

            result = await self._async_attempt_login(errors)
            if result is not None:
                return result

        default_email = ""
        if self._reauth_entry and self._reauth_entry.unique_id:
            default_email = self._reauth_entry.unique_id

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("email", default=default_email): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
        )
