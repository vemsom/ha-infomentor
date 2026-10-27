"""Stockholm BankID QR login and InfoMentor app authorization, over aiohttp.

The wire protocol is ported from the MIT-licensed ``infomentor-mcp`` project:

* ``src/infomentor_mcp/auth/stockholm_bankid.py`` — SSO chain, BankID order,
  status polling and the SAML completion.
* ``src/infomentor_mcp/auth/mobile_pairing.py`` — mobile discovery, app
  authorization and the token exchange.
* ``src/infomentor_mcp/login/web_qr.py`` — the rotating QR and its poll loop.

Nothing from that project is needed at runtime: this module talks to the same
endpoints with ``aiohttp`` and the Home Assistant client session.

Order of operations (verified live by that project, see its
``docs/mobile-auth.md``):

1. the guardian SSO starts a BankID order with ``initialize=qr``;
2. the QR is scanned and the order is polled until ``OK``;
3. the SAML chain that follows collects a Hub cookie session;
4. ``GetAuthenticationData`` on that session returns an ``authGuid``;
5. ``LoginOAuth2`` with the guid redirects to the app callback with a ``code``;
6. ``OAuth2/Token`` exchanges the code for access and refresh tokens.

Discovery needs an authenticated Hub session, so BankID always comes first —
the app grant cannot be started without it. No token, ``authGuid``, ``code``,
QR payload or request URL containing one is ever logged or put in a message.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import aiohttp

from .api import (
    HTML_HEADERS,
    REDIRECT_STATUSES,
    InfoMentorConnectionError,
    InfoMentorError,
    find_saml_form,
    parse_child_ids,
    parse_forms,
    validate_url,
)
from .const import (
    API_ORIGIN,
    AUTH_ORIGIN,
    CLIENT_ID,
    CLIENT_SECRET,
    HUB_ORIGIN,
    MAX_SSO_HOPS,
    REQUEST_TIMEOUT,
    SCOPE,
    TOKEN_PATH,
    USER_AGENT,
)

SSO_LOGIN_URL = "https://sso.infomentor.se/login.ashx?idp=stockholm_par"
AUTH_PATH = "/Authentication/Authentication/LoginOAuth2"
DISCOVERY_PATH = "/account/pair/GetAuthenticationData"
CALLBACK_URL = "InfomentorNotification://oauth2Callback"
CALLBACK_SCHEME = "infomentornotification"
CALLBACK_HOST = "oauth2callback"
# The value the inspected Hub app sends; the client still runs in Python.
DEVICE_PLATFORM = "Android"
DEVICE_NAME = "Home Assistant"
BANKID_STATES = frozenset({"PENDING", "USER_SIGN", "OK", "ERROR", "CANCELLED"})
REJECTED_STATES = frozenset({"ERROR", "CANCELLED"})

# A BankID order that is never scanned expires in about 30 seconds, but the
# whole attempt (start, scan, approve, SAML, grant) gets the same budget as
# the reference implementation.
LOGIN_TIMEOUT = 240
START_TIMEOUT = 30
POLL_INTERVAL = 1.0

_MBID_HREF = re.compile(
    r"""href\s*=\s*["']([^"']*NECSadc/mbid[^"']*)["']""", re.IGNORECASE
)


class BankIdError(InfoMentorError):
    """A BankID step failed; messages never contain protocol values."""


class BankIdRejectedError(BankIdError):
    """The order was cancelled or rejected in the BankID app."""


class BankIdProtocolError(BankIdError):
    """InfoMentor answered outside the verified protocol shape."""


class BankIdTokenError(BankIdError):
    """The authorization code was not exchanged for a usable token pair."""


class _InvalidPayload(Exception):
    """Internal: the response shape was not the verified one."""


@dataclass(frozen=True, slots=True)
class _Reply:
    """One fully read response; no connection is kept open."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError as err:
            raise BankIdProtocolError(
                "InfoMentor returned a response that was not JSON; body withheld"
            ) from err


def _endpoint_matches(value: object, origin: str, path: str) -> bool:
    """True when a discovered URL is exactly one of the verified endpoints."""
    if not isinstance(value, str) or not value:
        return False
    parsed = urlsplit(value)
    return (
        f"{parsed.scheme}://{parsed.netloc}" == origin
        and not parsed.fragment
        and parsed.path.rstrip("/") == path
    )


class BankIdProtocol:
    """One BankID login: SSO order, polling, Hub session, discovery, grant."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._page_url: str | None = None
        self._order: str | None = None
        self.qr_data: str | None = None
        self.launch_url: str | None = None
        self.auth_guid: str | None = None
        self.device_id = str(uuid4())
        self.access_token: str | None = None
        self.refresh_token: str | None = None

    # -- transport ---------------------------------------------------------- #

    async def _request(self, method: str, url: str, **kwargs: Any) -> _Reply:
        """Perform one request without following redirects."""
        kwargs.setdefault("allow_redirects", False)
        headers = {"User-Agent": USER_AGENT}
        headers.update(kwargs.pop("headers", None) or {})
        try:
            async with self._session.request(
                method,
                url,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                headers=headers,
                **kwargs,
            ) as response:
                return _Reply(
                    status=response.status,
                    headers=response.headers,
                    body=await response.read(),
                    url=str(response.url),
                )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise InfoMentorConnectionError(
                "Could not reach InfoMentor (network error or timeout)"
            ) from err

    # -- BankID ------------------------------------------------------------- #

    async def async_begin(self, method: str = "qr") -> None:
        """Walk the guardian SSO and start a BankID order.

        ``qr`` returns an animated QR payload to scan on another device;
        ``same_device`` returns an autostart link that opens the BankID app on
        the device that displays it.
        """
        if method not in {"qr", "same_device"}:
            raise ValueError("BankID method must be qr or same_device")
        url = SSO_LOGIN_URL
        verb, data = "GET", None
        for _ in range(MAX_SSO_HOPS):
            reply = await self._request(verb, url, data=data)
            if reply.status in REDIRECT_STATUSES:
                location = reply.headers.get("Location")
                if not location:
                    raise BankIdProtocolError("SSO redirect had no destination")
                url = validate_url(location, reply.url)
                verb, data = "GET", None
                continue
            if reply.status >= 400:
                raise InfoMentorConnectionError(
                    f"InfoMentor SSO returned HTTP {reply.status}"
                )
            page = reply.text()
            form = next(
                (item for item in parse_forms(page) if "SAMLRequest" in item.fields),
                None,
            )
            if form is not None:
                url = validate_url(form.action, reply.url)
                verb, data = "POST", form.fields
                continue
            href = _MBID_HREF.search(page)
            if href is not None:
                url = validate_url(unescape(href.group(1)), reply.url)
                verb, data = "GET", None
                continue
            url = reply.url
            break
        else:
            raise BankIdProtocolError("InfoMentor SSO exceeded its redirect limit")

        initialize = "qr" if method == "qr" else "url"
        separator = "&" if "?" in url else "?"
        reply = await self._request("GET", f"{url}{separator}initialize={initialize}")
        if reply.status >= 400:
            raise InfoMentorConnectionError(
                f"InfoMentor BankID start returned HTTP {reply.status}"
            )
        try:
            payload = reply.json()
            reference = payload["order"]
            value = payload["qrData" if method == "qr" else "token"]
            if not all(
                isinstance(item, str) and item.strip() for item in (reference, value)
            ):
                raise _InvalidPayload("Incomplete order")
        except (KeyError, TypeError, _InvalidPayload):
            raise BankIdProtocolError(
                "BankID did not start as expected; values withheld"
            ) from None
        self._page_url = url
        self._order = reference
        if method == "qr":
            self.qr_data = value
        else:
            self.launch_url = "https://app.bankid.com/?" + urlencode(
                {"autostarttoken": value, "redirect": "null"}
            )

    async def async_poll(self) -> str:
        """Return the current BankID state, refreshing the QR payload."""
        if self._page_url is None or self._order is None:
            raise BankIdProtocolError("No BankID order to poll")
        separator = "&" if "?" in self._page_url else "?"
        query = urlencode({"verifyorder": self._order, "_": int(time.time() * 1000)})
        reply = await self._request("GET", f"{self._page_url}{separator}{query}")
        if reply.status >= 400:
            raise InfoMentorConnectionError(
                f"InfoMentor BankID poll returned HTTP {reply.status}"
            )
        try:
            payload = reply.json()
            state = payload["state"]
            if state not in BANKID_STATES:
                raise _InvalidPayload("Unknown state")
            qr_data = payload.get("qrData")
            if qr_data is not None and not isinstance(qr_data, str):
                raise _InvalidPayload("Invalid QR payload")
        except (KeyError, TypeError, _InvalidPayload):
            raise BankIdProtocolError(
                "BankID answered outside the expected shape; values withheld"
            ) from None
        if qr_data:
            self.qr_data = qr_data
        return state

    async def async_finish(self) -> None:
        """Follow the SAML chain that follows an approved BankID order."""
        if self._page_url is None:
            raise BankIdProtocolError("No BankID order to finish")
        reply = await self._request("GET", self._page_url, headers=HTML_HEADERS)
        for _ in range(MAX_SSO_HOPS):
            if reply.status in REDIRECT_STATUSES:
                location = reply.headers.get("Location")
                if not location:
                    raise BankIdProtocolError("Sign-in redirect had no destination")
                reply = await self._request(
                    "GET", validate_url(location, reply.url), headers=HTML_HEADERS
                )
                continue
            if reply.status >= 400:
                raise InfoMentorConnectionError(
                    f"InfoMentor sign-in returned HTTP {reply.status}"
                )
            form = find_saml_form(reply.text())
            if form is None:
                return
            reply = await self._request(
                "POST",
                validate_url(form.action, reply.url),
                data=form.fields,
                headers=HTML_HEADERS,
            )
        raise BankIdProtocolError("InfoMentor sign-in chain exceeded its hop limit")

    async def async_verify_session(self) -> list[str]:
        """Prove the new session by reading the Hub's linked pupils."""
        reply = await self._request("GET", HUB_ORIGIN + "/", headers=HTML_HEADERS)
        if reply.status != 200:
            raise BankIdProtocolError(
                "The new BankID session was not accepted by the Hub "
                f"(HTTP {reply.status})"
            )
        child_ids = parse_child_ids(reply.text())
        if not child_ids:
            raise BankIdProtocolError("The new BankID session listed no linked pupils")
        return child_ids

    # -- app grant ---------------------------------------------------------- #

    async def async_discover(self) -> str:
        """Read the app authorization reference from the Hub session."""
        reply = await self._request("POST", HUB_ORIGIN + DISCOVERY_PATH, data=b"")
        if reply.status != 200:
            raise BankIdProtocolError(
                f"Mobile discovery returned HTTP {reply.status}"
            )
        try:
            payload = reply.json()
            if not isinstance(payload, dict):
                raise _InvalidPayload("Not an object")
            if not _endpoint_matches(payload["authenticationUrl"], AUTH_ORIGIN, AUTH_PATH):
                raise _InvalidPayload("Unexpected authorization endpoint")
            if not _endpoint_matches(payload["tokenUrl"], AUTH_ORIGIN, TOKEN_PATH):
                raise _InvalidPayload("Unexpected token endpoint")
            if not _endpoint_matches(payload["apiUrl"], API_ORIGIN, ""):
                raise _InvalidPayload("Unexpected API endpoint")
            if payload["clientId"] != CLIENT_ID:
                raise _InvalidPayload("Unexpected client")
            guids = parse_qs(urlsplit(payload["authenticationUrl"]).query).get(
                "authGuid", []
            )
            if len(guids) != 1 or not guids[0]:
                raise _InvalidPayload("Missing authorization reference")
        except (KeyError, TypeError, _InvalidPayload):
            raise BankIdProtocolError(
                "Mobile discovery did not match the verified app configuration"
            ) from None
        self.auth_guid = guids[0]
        return self.auth_guid

    async def async_authorize(self, *, device_name: str = DEVICE_NAME) -> str:
        """Start the app authorization and return the callback code."""
        if self.auth_guid is None:
            raise BankIdProtocolError("No authorization reference")
        params = {
            "authGuid": self.auth_guid,
            "DeviceIdentifier": self.device_id,
            "DeviceFriendlyName": device_name,
            "DevicePlatform": DEVICE_PLATFORM,
            "scope": SCOPE,
            "redirect_uri": CALLBACK_URL,
            "client_id": CLIENT_ID,
            "response_type": "code",
            "isSandBox": "false",
        }
        reply = await self._request("GET", AUTH_ORIGIN + AUTH_PATH, params=params)
        if reply.status not in REDIRECT_STATUSES:
            raise BankIdProtocolError(
                "App authorization did not redirect (HTTP "
                f"{reply.status}); response withheld"
            )
        location = reply.headers.get("Location")
        if not location:
            raise BankIdProtocolError("App authorization redirect had no destination")
        callback = urlsplit(location)
        query = parse_qs(callback.query)
        if (
            callback.scheme.lower() != CALLBACK_SCHEME
            or callback.netloc.lower() != CALLBACK_HOST
            or callback.path not in {"", "/"}
            or callback.fragment
            or "error" in query
            or len(query.get("code", [])) != 1
        ):
            raise BankIdProtocolError(
                "App authorization did not return the expected code callback"
            )
        code = query["code"][0]
        if not isinstance(code, str) or not code:
            raise BankIdProtocolError("App authorization returned an empty code")
        return code

    async def async_exchange(self, code: str) -> tuple[str, str]:
        """Exchange the authorization code for an access/refresh token pair."""
        reply = await self._request(
            "POST",
            AUTH_ORIGIN + TOKEN_PATH,
            data={
                "code": code,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": CALLBACK_URL,
                "grant_type": "authorization_code",
            },
        )
        if reply.status in (400, 401):
            raise BankIdTokenError(
                "InfoMentor rejected the authorization code "
                f"(HTTP {reply.status}); body withheld"
            )
        if reply.status != 200:
            raise InfoMentorConnectionError(
                f"InfoMentor token endpoint returned HTTP {reply.status}"
            )
        try:
            payload = reply.json()
            access = payload["access_token"]
            refresh = payload["refresh_token"]
            if not all(
                isinstance(value, str) and value.strip() for value in (access, refresh)
            ):
                raise _InvalidPayload("Missing token")
            if str(payload.get("token_type", "Bearer")).lower() != "bearer":
                raise ValueError("Unexpected token type")
            expires = payload.get("expires_in")
            if expires is not None and int(expires) < 0:
                raise _InvalidPayload("Invalid expiry")
        except (KeyError, TypeError, ValueError, _InvalidPayload):
            raise BankIdTokenError(
                "Token response was unexpected; body withheld"
            ) from None
        self.access_token = access
        self.refresh_token = refresh
        return access, refresh


UpdateCallback = Callable[[str | None, str, str | None], None]


class BankIdLogin:
    """One config-flow login attempt with a rotating QR and its status.

    The caller owns the HTTP session and creates the polling task, so the flow
    can hand it to ``async_show_progress``. ``async_wait`` never raises: the
    outcome is in ``status`` and ``error``.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        method: str = "qr",
        device_name: str = DEVICE_NAME,
        timeout: float = LOGIN_TIMEOUT,
        interval: float = POLL_INTERVAL,
        on_update: UpdateCallback | None = None,
        protocol_factory: Callable[[aiohttp.ClientSession], BankIdProtocol] = BankIdProtocol,
    ) -> None:
        self._session = session
        self._method = method
        self._device_name = device_name
        self._timeout = timeout
        self._interval = interval
        self._on_update = on_update
        self._protocol_factory = protocol_factory
        self.protocol: BankIdProtocol | None = None
        self.status = "starting"
        self.error: str | None = None
        self.qr_data: str | None = None
        self.launch_url: str | None = None
        self.refresh_token: str | None = None

    def _notify(self) -> None:
        if self._on_update is not None:
            self._on_update(self.qr_data, self.status, self.launch_url)

    async def async_start(self) -> None:
        """Start the BankID order; raises if InfoMentor cannot be reached."""
        self.protocol = self._protocol_factory(self._session)
        try:
            async with asyncio.timeout(START_TIMEOUT):
                await self.protocol.async_begin(self._method)
        except TimeoutError:
            raise BankIdProtocolError("BankID did not start in time") from None
        self.qr_data = self.protocol.qr_data
        self.launch_url = self.protocol.launch_url
        self.status = "waiting"
        self._notify()

    async def async_wait(self) -> None:
        """Poll until approval, rejection or timeout, then fetch the tokens."""
        protocol = self.protocol
        if protocol is None:
            self.status, self.error = "failed", "failed"
            return
        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    await asyncio.sleep(self._interval)
                    state = await protocol.async_poll()
                    if state == "OK":
                        break
                    if state in REJECTED_STATES:
                        raise BankIdRejectedError("BankID order was not approved")
                    self.qr_data = protocol.qr_data
                    self.status = "user_sign" if state == "USER_SIGN" else "waiting"
                    self._notify()
            self.qr_data = None
            self.status = "verifying"
            self._notify()
            await protocol.async_finish()
            await protocol.async_verify_session()
            await protocol.async_discover()
            code = await protocol.async_authorize(device_name=self._device_name)
            await protocol.async_exchange(code)
            self.refresh_token = protocol.refresh_token
            self.status = "complete"
        except TimeoutError:
            self.status, self.error = "timeout", "timeout"
        except asyncio.CancelledError:
            self.status = "cancelled"
            self.error = "cancelled"
            self._notify()
            raise
        except BankIdRejectedError:
            self.status, self.error = "cancelled", "cancelled"
        except BankIdTokenError:
            self.status, self.error = "failed", "invalid_auth"
        except InfoMentorConnectionError:
            self.status, self.error = "failed", "connection"
        except InfoMentorError:
            self.status, self.error = "failed", "failed"
        finally:
            self.qr_data = None
            self._notify()
