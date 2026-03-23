"""Ring REST client for OAuth authentication and API requests.

Port of the TypeScript RingRestClient. Handles:
- OAuth token exchange (refresh token or email/password)
- 2FA challenges (412 response)
- Session creation with Ring servers
- Authenticated API requests with automatic token refresh
- Refresh token rotation and persistence

The auth protocol:
1. Exchange refresh token (or email+password) for an OAuth access token
   via https://oauth.ring.com/oauth/token
2. Create a session via POST to clients_api/session
3. Use the access token as Bearer token on all subsequent requests
4. When the token expires (~1hr), refresh automatically

The refresh token is base64-encoded JSON: { rt: "actual_token", hid: "hardware_id" }
"""

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Callable

import aiohttp

from ..const import CLIENT_API_BASE, API_VERSION

_LOGGER = logging.getLogger(__name__)


class RingAuthError(Exception):
    """Ring authentication error."""


class Ring2FARequired(Exception):
    """2FA verification code required."""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        super().__init__(prompt)


def _from_base64(s: str) -> str:
    return base64.b64decode(s).decode("ascii")


def _to_base64(s: str) -> str:
    return base64.b64encode(s.encode()).decode("ascii")


def _parse_auth_config(raw_token: str | None) -> dict | None:
    """Parse a refresh token into its components.

    Ring refresh tokens are base64-encoded JSON containing the actual
    token and a hardware ID. Older/raw tokens are just the token string.
    """
    if not raw_token:
        return None
    try:
        config = json.loads(_from_base64(raw_token))
        if config.get("rt"):
            return config
        return {"rt": raw_token}
    except Exception:
        return {"rt": raw_token}


class RingRestClient:
    """Lean Ring REST client for OAuth and authenticated requests."""

    def __init__(
        self,
        refresh_token: str | None = None,
        email: str | None = None,
        password: str | None = None,
        on_token_update: Callable[[str], None] | None = None,
    ) -> None:
        self.refresh_token = refresh_token
        self._email = email
        self._password = password
        self._on_token_update = on_token_update
        self._auth_config = _parse_auth_config(refresh_token)
        self._hardware_id = (
            self._auth_config.get("hid", str(uuid.uuid4()))
            if self._auth_config
            else str(uuid.uuid4())
        )
        self._access_token: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._session_created = False
        self.prompt_for_2fa: str | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def authenticate(self, two_factor_code: str | None = None) -> str:
        """Authenticate with Ring and return the refresh token.

        Handles 2FA challenges (raises Ring2FARequired with prompt),
        token rotation, and credential fallback.
        """
        session = await self._get_session()

        grant_data: dict[str, str]
        if self._auth_config and self._auth_config.get("rt") and not two_factor_code:
            grant_data = {
                "grant_type": "refresh_token",
                "refresh_token": self._auth_config["rt"],
            }
        elif self._email and self._password:
            grant_data = {
                "grant_type": "password",
                "password": self._password,
                "username": self._email,
            }
        else:
            raise RingAuthError("No credentials available for authentication")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "2fa-support": "true",
            "2fa-code": two_factor_code or "",
            "hardware_id": self._hardware_id,
            "User-Agent": "android:com.ringapp",
        }

        payload = {
            "client_id": "ring_official_android",
            "scope": "client",
            **grant_data,
        }

        try:
            async with session.post(
                "https://oauth.ring.com/oauth/token",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 412:
                    body = await resp.json()
                    if "tsv_state" in body:
                        tsv_state = body["tsv_state"]
                        phone = body.get("phone", "")
                        if tsv_state == "totp":
                            prompt = "from your authenticator app"
                        else:
                            prompt = f"sent to {phone} via {tsv_state}"
                        self.prompt_for_2fa = f"Please enter the code {prompt}"
                    else:
                        self.prompt_for_2fa = (
                            "Please enter the code sent to your text/email"
                        )
                    raise Ring2FARequired(self.prompt_for_2fa)

                if resp.status == 400:
                    body = await resp.json()
                    error = body.get("error", "")
                    if str(error).startswith("Verification Code"):
                        self.prompt_for_2fa = (
                            "Invalid code entered. Please try again."
                        )
                        raise Ring2FARequired(self.prompt_for_2fa)
                    raise RingAuthError(f"Authentication failed: {error}")

                if resp.status != 200:
                    text = await resp.text()
                    if grant_data.get("grant_type") == "refresh_token":
                        self._auth_config = None
                        self.refresh_token = None
                        return await self.authenticate(two_factor_code)
                    raise RingAuthError(
                        f"Authentication failed ({resp.status}): {text}"
                    )

                data = await resp.json()

        except (Ring2FARequired, RingAuthError):
            raise
        except aiohttp.ClientError as err:
            raise RingAuthError(f"Network error during authentication: {err}") from err

        self._access_token = data["access_token"]

        self._auth_config = {
            **(self._auth_config or {}),
            "rt": data["refresh_token"],
            "hid": self._hardware_id,
        }
        self.refresh_token = _to_base64(json.dumps(self._auth_config))

        if self._on_token_update:
            self._on_token_update(self.refresh_token)

        return self.refresh_token

    async def _ensure_access_token(self) -> None:
        if not self._access_token:
            await self.authenticate()

    async def _ensure_session(self) -> None:
        """Create a Ring session (registers this device with Ring servers)."""
        if self._session_created:
            return
        await self._ensure_access_token()
        session = await self._get_session()
        try:
            async with session.post(
                f"{CLIENT_API_BASE}session",
                json={
                    "device": {
                        "hardware_id": self._hardware_id,
                        "metadata": {
                            "api_version": API_VERSION,
                            "device_model": "ring-client-api",
                        },
                        "os": "android",
                    }
                },
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 401:
                    self._access_token = None
                    await self._ensure_access_token()
                    return await self._ensure_session()
                self._session_created = True
        except aiohttp.ClientError as err:
            _LOGGER.warning("Session creation failed: %s", err)
            self._session_created = True

    async def request(self, url: str) -> Any:
        """Make an authenticated GET request to a Ring API endpoint."""
        await self._ensure_session()
        session = await self._get_session()

        for _attempt in range(3):
            try:
                async with session.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "hardware_id": self._hardware_id,
                        "User-Agent": "android:com.ringapp",
                        "Accept": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 401:
                        self._access_token = None
                        self._session_created = False
                        await self._ensure_session()
                        continue
                    if resp.status == 429:
                        retry_after = resp.headers.get("retry-after", "60")
                        wait = (
                            int(retry_after) if retry_after.isdigit() else 60
                        )
                        _LOGGER.warning("Rate limited, waiting %s seconds", wait)
                        await asyncio.sleep(wait + 1)
                        continue
                    if resp.status == 504:
                        await asyncio.sleep(5)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError:
                if _attempt == 2:
                    raise
                await asyncio.sleep(5)

        raise RingAuthError(f"Request to {url} failed after retries")
