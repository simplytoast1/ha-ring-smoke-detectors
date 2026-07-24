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

Error model:
- RingApiError: transient or generic API failure (network, 5xx). Safe to retry.
- RingAuthError: Ring definitively rejected the credentials or token.
  Callers should treat this as "reauthentication required".
- Ring2FARequired: a subclass of RingAuthError raised during interactive
  login when Ring wants a verification code.
"""

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import aiohttp

from ..const import CLIENT_API_BASE, API_VERSION

_LOGGER = logging.getLogger(__name__)

OAUTH_URL = "https://oauth.ring.com/oauth/token"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Ring can send very large retry-after values when it rate-limits an
# account. Sleeping that long inside a coordinator refresh or config
# entry setup would hang Home Assistant, so cap what we honor.
MAX_RATE_LIMIT_WAIT = 60


class RingApiError(Exception):
    """Generic Ring API failure (network trouble, server errors)."""


class RingAuthError(RingApiError):
    """Ring definitively rejected the credentials or refresh token."""


class Ring2FARequired(RingAuthError):
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
    """Lean Ring REST client for OAuth and authenticated requests.

    The aiohttp session is owned by the caller (Home Assistant's shared
    session); this class never creates or closes sessions.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str | None = None,
        email: str | None = None,
        password: str | None = None,
        on_token_update: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
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
        self._session_created = False
        self.prompt_for_2fa: str | None = None
        # Ring refresh tokens are single-use; concurrent refreshes would
        # double-spend one and invalidate the account's stored token.
        self._auth_lock = asyncio.Lock()

    async def authenticate(self, two_factor_code: str | None = None) -> str:
        """Authenticate with Ring and return the refresh token.

        Handles 2FA challenges (raises Ring2FARequired with prompt),
        token rotation, and credential fallback. A definitive rejection
        raises RingAuthError; transient failures raise RingApiError and
        leave the stored refresh token intact so a later retry can work.
        """
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
            async with self._session.post(
                OAUTH_URL,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 412:
                    body = await self._safe_json(resp)
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
                    body = await self._safe_json(resp)
                    error = body.get("error", "")
                    if str(error).startswith("Verification Code"):
                        self.prompt_for_2fa = (
                            "Invalid code entered. Please try again."
                        )
                        raise Ring2FARequired(self.prompt_for_2fa)

                if resp.status in (400, 401, 403):
                    # Definitive rejection. If we were using a stored
                    # refresh token and also have credentials (config
                    # flow), fall back to a password login.
                    if (
                        grant_data["grant_type"] == "refresh_token"
                        and self._email
                        and self._password
                    ):
                        self._auth_config = None
                        self.refresh_token = None
                        return await self.authenticate(two_factor_code)
                    text = await resp.text()
                    raise RingAuthError(
                        f"Ring rejected the credentials ({resp.status}): "
                        f"{text[:200]}"
                    )

                if resp.status != 200:
                    # Transient server trouble (5xx, unexpected status).
                    # Keep the stored refresh token so retries can succeed
                    # once Ring recovers.
                    text = await resp.text()
                    raise RingApiError(
                        f"Ring OAuth endpoint returned {resp.status}: "
                        f"{text[:200]}"
                    )

                data = await resp.json()

        except RingApiError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RingApiError(
                f"Network error during authentication: {err}"
            ) from err

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

    @staticmethod
    async def _safe_json(resp: aiohttp.ClientResponse) -> dict:
        """Parse a response body as JSON, returning {} on any mismatch."""
        try:
            body = await resp.json(content_type=None)
        except (aiohttp.ClientError, json.JSONDecodeError, ValueError):
            return {}
        return body if isinstance(body, dict) else {}

    async def _ensure_access_token(self) -> None:
        if self._access_token:
            return
        async with self._auth_lock:
            # Another caller may have refreshed while we waited
            if not self._access_token:
                await self.authenticate()

    async def _ensure_session(self) -> None:
        """Create a Ring session (registers this device with Ring servers).

        Session creation is best-effort: network failures are logged and
        skipped. A 401 triggers exactly one re-authentication attempt;
        a second 401 is surfaced as an auth failure rather than recursing.
        """
        if self._session_created:
            return

        for attempt in range(2):
            await self._ensure_access_token()
            try:
                async with self._session.post(
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
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status == 401:
                        self._access_token = None
                        if attempt == 0:
                            continue
                        raise RingAuthError(
                            "Ring session rejected the access token even "
                            "after re-authentication"
                        )
                    if resp.status >= 400:
                        _LOGGER.warning(
                            "Ring session creation returned %s, "
                            "continuing without it",
                            resp.status,
                        )
                    self._session_created = True
                    return
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.warning(
                    "Ring session creation failed (%s), continuing without it",
                    err,
                )
                self._session_created = True
                return

    async def request(self, url: str) -> Any:
        """Make an authenticated GET request to a Ring API endpoint.

        A 401 triggers one re-authentication without consuming a retry
        attempt; a second 401 after that means the fresh token is being
        rejected too and raises RingAuthError so the caller can start a
        reauth flow. A 401 that never gets a post-reauth attempt (e.g.
        the token expired on the final retry) stays a transient
        RingApiError, because the stored refresh token may be fine.
        """
        await self._ensure_session()

        last_err: Exception | None = None
        last_status: int | None = None
        reauthed = False
        attempts_left = 3
        while attempts_left > 0:
            try:
                async with self._session.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "hardware_id": self._hardware_id,
                        "User-Agent": "android:com.ringapp",
                        "Accept": "application/json",
                    },
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    last_status = resp.status
                    if resp.status == 401:
                        if reauthed:
                            raise RingAuthError(
                                f"Ring kept rejecting the access token "
                                f"for {url} after re-authentication"
                            )
                        self._access_token = None
                        self._session_created = False
                        await self._ensure_session()
                        reauthed = True
                        continue
                    if resp.status == 429:
                        retry_after = resp.headers.get("retry-after", "")
                        wait = min(
                            int(retry_after)
                            if retry_after.isdigit()
                            else MAX_RATE_LIMIT_WAIT,
                            MAX_RATE_LIMIT_WAIT,
                        )
                        _LOGGER.warning(
                            "Rate limited by Ring, waiting %s seconds", wait
                        )
                        attempts_left -= 1
                        await asyncio.sleep(wait + 1)
                        continue
                    if resp.status == 504:
                        attempts_left -= 1
                        await asyncio.sleep(5)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, TimeoutError) as err:
                last_err = err
                attempts_left -= 1
                if attempts_left <= 0:
                    raise RingApiError(
                        f"Request to {url} failed: {err}"
                    ) from err
                await asyncio.sleep(5)

        raise RingApiError(
            f"Request to {url} failed after retries"
            + (f" (last status: {last_status})" if last_status else "")
            + (f" (last error: {last_err})" if last_err else "")
        )
