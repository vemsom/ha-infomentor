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
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Temporary: Home Assistant's in-memory log is not readable from the add-on, so
# the login writes its short state sequence to a file while we debug live.
TRACE_FILE = "/config/infomentor-login.log"


def _trace(message: str) -> None:
    """Append one line to the login trace (no secrets, state names only)."""
    try:
        with open(TRACE_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


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
HUB_HOST = urlsplit(HUB_ORIGIN).netloc

_META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["'][^"']*url=([^"'\s>]+)""",
    re.IGNORECASE,
)
_JS_LOCATION = re.compile(
    r"""location(?:\.href)?\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)


def _dump_page(html: str) -> None:
    """Keep the last unparsed sign-in page for inspection (his own machine)."""
    try:
        with open("/config/infomentor-page.html", "w", encoding="utf-8") as handle:
            handle.write(html[:200000])
    except OSError:
        pass


def find_client_redirect(html: str) -> str | None:
    """Return a meta-refresh or JavaScript redirect target, if the page has one."""
    for pattern in (_META_REFRESH, _JS_LOCATION):
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


REJECTED_STATES = frozenset({"ERROR", "CANCELLED"})

# A BankID order that is never scanned expires in about 30 seconds, but the
# whole attempt (start, scan, approve, SAML, grant) gets the same budget as
# the reference implementation.
LOGIN_TIMEOUT = 240
MAX_RENEWALS = 6
START_TIMEOUT = 30
POLL_INTERVAL = 1.0
# The page's script waits three seconds after it sees OK before reloading.
RELOAD_DELAY = 3.0

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
        self._start_url: str | None = None
        self._method = "qr"
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
                cookies = response.headers.getall("Set-Cookie", [])
                if cookies:
                    names = [c.split("=", 1)[0].strip() for c in cookies]
                    _trace(
                        f"cookie {urlsplit(str(response.url)).netloc} "
                        f"{response.status} {names}"
                    )
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

        self._method = method
        # The page's own script asks for both flavours with initialize=bankid:
        # qrData for the animated QR (bankid_scripts.js:455) and token for the
        # autostart link (:429). "url" is a value only this client sends, and
        # the reference client passes "bankid" here too.
        initialize = "qr" if method == "qr" else "bankid"
        separator = "&" if "?" in url else "?"
        self._start_url = f"{url}{separator}initialize={initialize}"
        reply = await self._request("GET", self._start_url)
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
        reply = await self._request(
            "GET",
            f"{self._page_url}{separator}{query}",
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "Referer": self._page_url,
            },
        )
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
        # The page's own script polls by AJAX, so its address bar keeps the
        # bare page URL; that is what a reload requests too.
        first_url = self._page_url
        headers = dict(HTML_HEADERS)
        headers["Referer"] = self._page_url
        reply = await self._request("GET", first_url, headers=headers)
        _trace(f"finish start {urlsplit(reply.url).netloc} {reply.status} (start-url)")
        settle = 0
        for _ in range(MAX_SSO_HOPS):
            if reply.status in REDIRECT_STATUSES:
                location = reply.headers.get("Location")
                if not location:
                    raise BankIdProtocolError("Sign-in redirect had no destination")
                target = validate_url(location, reply.url)
                _trace(
                    f"hop {reply.status} -> {urlsplit(target).netloc}{urlsplit(target).path[:60]}"
                )
                reply = await self._request("GET", target, headers=HTML_HEADERS)
                continue
            if reply.status >= 400:
                raise InfoMentorConnectionError(
                    f"InfoMentor sign-in returned HTTP {reply.status}"
                )
            text = reply.text()
            form = find_saml_form(text)
            netloc = urlsplit(reply.url).netloc
            _trace(
                f"page {netloc}{urlsplit(reply.url).path[:40]} {reply.status} "
                f"form={'yes' if form else 'no'} len={len(reply.body)} "
                f"saml={'SAML' in text} meta={'http-equiv' in text.lower()} "
                f"js={'location' in text.lower()} forms={text.lower().count('<form')}"
            )
            if form is None and netloc.endswith(HUB_HOST):
                return
            if form is None:
                # Stockholm's SSO ends on a "continue" page whose single form
                # carries the next hop. It is not a SAML form, but posting it
                # is what the browser does.
                forms = parse_forms(text)
                _trace(
                    f"parsed forms={len(forms)} actions={[urlsplit(f.action).path[:30] for f in forms]}"
                )
                if len(forms) == 1 and forms[0].action:
                    form = forms[0]
                    _trace(
                        "single form used fields=" + ",".join(sorted(form.fields))[:80]
                    )
            if form is None:
                _dump_page(text)
                location = find_client_redirect(text)
                _trace(f"client redirect={bool(location)}")
                if location is not None:
                    _trace("client-side redirect followed")
                    reply = await self._request(
                        "GET", validate_url(location, reply.url), headers=HTML_HEADERS
                    )
                    continue
                # The SSO sometimes hands back an intermediate page that only
                # turns into the next hop a moment later.
                if settle < 2:
                    settle += 1
                    await asyncio.sleep(3.0)
                    _trace("retry same page")
                    reply = await self._request("GET", reply.url, headers=HTML_HEADERS)
                    continue
                return
            reply = await self._request(
                "POST",
                validate_url(form.action, reply.url),
                data=form.fields,
                headers=HTML_HEADERS,
            )
        raise BankIdProtocolError("InfoMentor sign-in chain exceeded its hop limit")

    async def async_verify_session(self) -> list[str]:
        """Prove the new session the way the working client does.

        A read of the Hub page proves nothing on its own: it answers 200 with an
        empty shell when there is no session. The timetable endpoint only
        returns data to an authenticated cookie, so that is the real gate.
        """
        today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
        reply = await self._request(
            "POST",
            HUB_ORIGIN + "/timetable/timetable/appData",
            json={
                "startDate": today.isoformat(),
                "endDate": (today + timedelta(days=1)).isoformat(),
            },
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        _trace(f"verify appData {reply.status} len={len(reply.body)}")
        if reply.status != 200:
            raise BankIdProtocolError(
                "The new BankID session was not accepted by the Hub "
                f"(HTTP {reply.status})"
            )
        try:
            payload = reply.json()
        except BankIdProtocolError:
            raise BankIdProtocolError("The Hub answered with something other than JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise BankIdProtocolError("The Hub did not return timetable data")

        page = await self._request("GET", HUB_ORIGIN + "/", headers=HTML_HEADERS)
        child_ids = parse_child_ids(page.text()) if page.status == 200 else []
        _trace(f"verify pupils={len(child_ids)}")
        return child_ids

    # -- app grant ---------------------------------------------------------- #

    async def async_discover(self) -> str:
        """Read the app authorization reference from the Hub session."""
        reply = await self._request("POST", HUB_ORIGIN + DISCOVERY_PATH, data=b"")
        if reply.status != 200:
            raise BankIdProtocolError(f"Mobile discovery returned HTTP {reply.status}")
        try:
            payload = reply.json()
            if not isinstance(payload, dict):
                raise _InvalidPayload("Not an object")
            if not _endpoint_matches(
                payload["authenticationUrl"], AUTH_ORIGIN, AUTH_PATH
            ):
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
        reload_delay: float = RELOAD_DELAY,
        on_update: UpdateCallback | None = None,
        protocol_factory: Callable[
            [aiohttp.ClientSession], BankIdProtocol
        ] = BankIdProtocol,
    ) -> None:
        self._session = session
        self._method = method
        self._device_name = device_name
        self._timeout = timeout
        self._interval = interval
        self._reload_delay = reload_delay
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
            renewals = 0
            async with asyncio.timeout(self._timeout):
                while True:
                    await asyncio.sleep(self._interval)
                    state = await protocol.async_poll()
                    _LOGGER.debug("BankID poll state: %s", state)
                    _trace(f"poll {state}")
                    if state == "OK":
                        break
                    if state in REJECTED_STATES:
                        # Stockholm drops an unscanned order after about
                        # thirty seconds. Rather than kill the page while the
                        # user is still fetching a second screen, start a new
                        # order and keep the same one-time link alive.
                        if renewals < MAX_RENEWALS:
                            renewals += 1
                            _trace(f"order ended ({state}); starting a new one")
                            self.qr_data = None
                            self._notify()
                            await protocol.async_begin(self._method)
                            self.qr_data = protocol.qr_data
                            self.launch_url = protocol.launch_url
                            self._notify()
                            continue
                        raise BankIdRejectedError("BankID order was not approved")
                    self.qr_data = protocol.qr_data
                    self.status = "user_sign" if state == "USER_SIGN" else "waiting"
                    self._notify()
            self.qr_data = None
            self.status = "verifying"
            self._notify()
            _trace("approved")
            # The page's own script stops polling the moment it sees OK and
            # reloads three seconds later (bankid_scripts.js, case "OK"). The
            # handover that SiteMinder builds for that reload is good for one
            # request, so a verifyorder poll after approval — a request the
            # browser never makes — burns it and the reload lands back on the
            # BankID page with no SAML form.
            await asyncio.sleep(self._reload_delay)
            await protocol.async_finish()
            _trace("saml ok")
            await protocol.async_verify_session()
            _trace("session ok")
            await protocol.async_discover()
            _trace("discovery ok")
            code = await protocol.async_authorize(device_name=self._device_name)
            _trace("authorized")
            await protocol.async_exchange(code)
            _trace("tokens ok")
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
            _trace("rejected by BankID")
            self.status, self.error = "cancelled", "cancelled"
        except BankIdTokenError:
            _trace("token exchange failed")
            self.status, self.error = "failed", "invalid_auth"
        except InfoMentorConnectionError:
            self.status, self.error = "failed", "connection"
        except InfoMentorError:
            _trace("infomentor error")
            self.status, self.error = "failed", "failed"
        except Exception:  # never leave the dialog spinning
            _LOGGER.exception("BankID login stopped on an unexpected error")
            self.status, self.error = "failed", "failed"
        finally:
            self.qr_data = None
            self._notify()
