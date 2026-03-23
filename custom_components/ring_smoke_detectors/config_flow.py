"""Config flow for Ring Smoke Detectors.

Handles the Ring authentication flow with 2FA support:
1. User enters email + password
2. If 2FA is required, user enters verification code
3. Refresh token is stored in config entry
"""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN, CONF_REFRESH_TOKEN
from .ring_api.auth import RingRestClient, RingAuthError, Ring2FARequired

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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: email + password login."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input["email"]
            self._password = user_input["password"]

            self._rest_client = RingRestClient(
                email=self._email,
                password=self._password,
            )

            try:
                refresh_token = await self._rest_client.authenticate()
                await self._rest_client.close()

                await self.async_set_unique_id(self._email.lower())
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Ring ({self._email})",
                    data={CONF_REFRESH_TOKEN: refresh_token},
                )

            except Ring2FARequired:
                self._2fa_prompt = self._rest_client.prompt_for_2fa or (
                    "Enter verification code"
                )
                return await self.async_step_2fa()

            except RingAuthError as err:
                _LOGGER.error("Ring authentication failed: %s", err)
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unexpected error during Ring login")
                errors["base"] = "unknown"

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
            code = user_input["code"]

            try:
                refresh_token = await self._rest_client.authenticate(
                    two_factor_code=code
                )
                await self._rest_client.close()

                await self.async_set_unique_id(self._email.lower())
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Ring ({self._email})",
                    data={CONF_REFRESH_TOKEN: refresh_token},
                )

            except Ring2FARequired:
                self._2fa_prompt = self._rest_client.prompt_for_2fa or (
                    "Invalid code. Please try again."
                )
                errors["base"] = "invalid_2fa_code"

            except RingAuthError as err:
                _LOGGER.error("Ring 2FA failed: %s", err)
                errors["base"] = "invalid_auth"

            except Exception:
                _LOGGER.exception("Unexpected error during 2FA")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="2fa",
            data_schema=vol.Schema(
                {
                    vol.Required("code"): str,
                }
            ),
            description_placeholders={"2fa_prompt": self._2fa_prompt},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when the token expires."""
        return await self.async_step_user()
