"""Shared test fixtures for the InfoMentor integration tests."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from aiohttp import web
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

pytest_plugins = "pytest_homeassistant_custom_component"

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components in every test."""
    yield


@pytest.fixture(autouse=True)
async def setup_http(hass):
    """Set up the http component, which the integration declares as dependency.

    The BankID QR view lives on ``hass.http``; in Home Assistant the manifest
    dependency guarantees it is set up, in tests it has to be done explicitly.
    Home Assistant keeps the router unfrozen so components discovered after
    boot can still register views (``http.HomeAssistantHTTP.start``); the
    aiohttp test server would otherwise freeze it on start.
    """
    assert await async_setup_component(hass, "http", {})
    hass.http.app._router.freeze = lambda: None
    return hass.http


# --------------------------------------------------------------------------- #
# A local fake of the Swedish hosts, for the BankID QR login chain
# --------------------------------------------------------------------------- #

SSO_FORM_PAGE = """
<html><body>
<form method="post" action="/sso/guardian">
  <input type="hidden" name="SAMLRequest" value="request"/>
</form>
</body></html>
"""

MBID_PAGE = """
<html><body><a href="/sso/NECSadc/mbid">BankID</a></body></html>
"""

BANKID_PAGE = """
<html><body><form method="post"><p>Logga in med BankID</p></form></body></html>
"""

SAML_PAGE = """
<html><body>
<form method="post" action="/sso/consume">
  <input type="hidden" name="SAMLResponse" value="response"/>
</form>
</body></html>
"""

HUB_PAGE = """
<html><body>
<script>var config = { selectedPupilName : 'Ella' };</script>
<a href="/Account/PupilSwitcher/SwitchPupil/12345">Ella</a>
</body></html>
"""


class FakeInfoMentorHosts:
    """The minimum of the Swedish hosts needed by one QR login."""

    def __init__(self) -> None:
        self.states = ["USER_SIGN", "OK"]
        self.approved = False
        self.orders = 0
        self.token_requests: list[dict[str, str]] = []
        self.authorize_params: dict[str, str] | None = None
        self.token_status = 200
        self.discovery_authentication_url: str | None = None
        self.initialize_calls: list[str] = []

    def routes(self):
        return [
            web.get("/sso/login", self.sso_login),
            web.post("/sso/guardian", self.guardian),
            web.get("/sso/NECSadc/mbid", self.bankid),
            web.post("/sso/consume", self.consume),
            web.get("/", self.hub_root),
            web.post("/timetable/timetable/appData", self.hub_app_data),
            web.post("/account/pair/GetAuthenticationData", self.discovery),
            web.get("/Authentication/Authentication/LoginOAuth2", self.authorize),
            web.post("/Authentication/OAuth2/Token", self.token),
        ]

    async def sso_login(self, request: web.Request) -> web.Response:
        return web.Response(text=SSO_FORM_PAGE, content_type="text/html")

    async def guardian(self, request: web.Request) -> web.Response:
        assert (await request.post())["SAMLRequest"] == "request"
        return web.Response(text=MBID_PAGE, content_type="text/html")

    async def bankid(self, request: web.Request) -> web.Response:
        if "initialize" in request.query:
            self.initialize_calls.append(request.query["initialize"])
            self.orders += 1
            return web.json_response(
                {
                    "order": f"ORDER{self.orders}",
                    "qrData": "bankid.QR1",
                    "token": "AUTOSTART",
                }
            )
        if "verifyorder" in request.query:
            assert request.query["verifyorder"] == f"ORDER{self.orders}"
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            if state == "OK":
                self.approved = True
            return web.json_response({"state": state, "qrData": "bankid.QR2"})
        if self.approved:
            return web.Response(text=SAML_PAGE, content_type="text/html")
        return web.Response(text=BANKID_PAGE, content_type="text/html")

    async def consume(self, request: web.Request) -> web.Response:
        assert (await request.post())["SAMLResponse"] == "response"
        response = web.Response(status=302, headers={"Location": "/"})
        response.set_cookie("HUBSSO", "ok")
        return response

    async def hub_root(self, request: web.Request) -> web.Response:
        assert request.cookies.get("HUBSSO") == "ok"
        return web.Response(text=HUB_PAGE, content_type="text/html")

    async def hub_app_data(self, request: web.Request) -> web.Response:
        # The real Hub only answers this for an authenticated cookie session.
        if request.cookies.get("HUBSSO") != "ok":
            return web.Response(status=302, headers={"Location": "/"})
        return web.json_response({"items": []})

    async def discovery(self, request: web.Request) -> web.Response:
        assert request.cookies.get("HUBSSO") == "ok"
        origin = str(request.url).split("/account/")[0]
        return web.json_response(
            {
                "authenticationUrl": self.discovery_authentication_url
                or f"{origin}/Authentication/Authentication/LoginOAuth2?authGuid=GUID",
                "tokenUrl": f"{origin}/Authentication/OAuth2/Token",
                "revokeRefreshTokenUrl": (
                    f"{origin}/Authentication/OAuth2/RevokeRefreshToken?authGuid=GUID"
                ),
                "apiUrl": origin,
                "clientId": "notificationapp",
                "expirationTime": "2026-10-28T00:00:00",
            }
        )

    async def authorize(self, request: web.Request) -> web.Response:
        self.authorize_params = dict(request.query)
        return web.Response(
            status=302,
            headers={"Location": "InfomentorNotification://oauth2Callback?code=CODE1"},
        )

    async def token(self, request: web.Request) -> web.Response:
        form = {key: str(value) for key, value in (await request.post()).items()}
        self.token_requests.append(form)
        if self.token_status != 200:
            return web.json_response(
                {"error": "invalid_grant"}, status=self.token_status
            )
        return web.json_response(
            {
                "access_token": "ACCESS",
                "refresh_token": "REFRESH",
                "token_type": "Bearer",
                "expires_in": 600,
            }
        )


@pytest.fixture
async def fake_hosts(aiohttp_client, monkeypatch, socket_enabled):
    """Point the BankID module at a local fake over plain HTTP."""
    from urllib.parse import urljoin

    from custom_components.infomentor import bankid as bankid_module

    fake = FakeInfoMentorHosts()
    app = web.Application()
    app.add_routes(fake.routes())
    client = await aiohttp_client(app)
    origin = str(client.make_url("")).rstrip("/")
    monkeypatch.setattr(bankid_module, "AUTH_ORIGIN", origin)
    monkeypatch.setattr(bankid_module, "API_ORIGIN", origin)
    monkeypatch.setattr(bankid_module, "HUB_ORIGIN", origin)
    monkeypatch.setattr(bankid_module, "SSO_LOGIN_URL", origin + "/sso/login")
    # The fake runs over plain HTTP; the https allowlist has its own tests.
    monkeypatch.setattr(
        bankid_module,
        "validate_url",
        lambda url, base=None: urljoin(base, url) if base else url,
    )
    return fake, client


@pytest.fixture(autouse=True)
def mock_info_mentor_api(monkeypatch):
    """Replace the real API class with an in-memory fake in every module."""

    class FakeApi:
        """Stands in for InfoMentorApi; no network access at all."""

        instances: ClassVar[list[FakeApi]] = []
        children: ClassVar[list[dict[str, str]]] = [
            {"id": "pupil-1", "name": "Ella"},
            {"id": "pupil-2", "name": "Olle"},
        ]
        fetch_error: ClassVar[Exception | None] = None
        validate_error: ClassVar[Exception | None] = None

        def __init__(self, session, refresh_token, *, on_token_refresh=None):
            self.session = session
            self._refresh_token = refresh_token
            self.on_token_refresh = on_token_refresh
            self.validated = False
            self.fetched: list[list[str]] = []
            FakeApi.instances.append(self)

        @property
        def refresh_token(self):
            return self._refresh_token

        async def async_validate(self):
            from custom_components.infomentor.api import Child

            if FakeApi.validate_error is not None:
                raise FakeApi.validate_error
            self.validated = True
            return [Child(id=item["id"], name=item["name"]) for item in self.children]

        async def async_fetch(self, child_ids, **kwargs):
            from custom_components.infomentor.api import (
                Child,
                ChildData,
                NewsItem,
                Notification,
                SchoolEvent,
            )

            self.fetched.append(list(child_ids))
            if FakeApi.fetch_error is not None:
                raise FakeApi.fetch_error
            tomorrow = dt_util.now() + timedelta(days=1)
            event_start = tomorrow.replace(hour=8, minute=15, second=0, microsecond=0)
            event_end = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
            result = []
            for child_id in child_ids:
                name = next(
                    item["name"] for item in self.children if item["id"] == child_id
                )
                result.append(
                    ChildData(
                        child=Child(id=child_id, name=name),
                        notifications=(
                            Notification(
                                id="n1",
                                title="Ny läxa i matematik",
                                state="New",
                                sent_at="2026-10-27T07:00:00",
                                notification_type="Homework",
                            ),
                            Notification(
                                id="n2",
                                title="Redan läst",
                                state="Seen",
                                sent_at="2026-10-26T07:00:00",
                                notification_type="Info",
                            ),
                        ),
                        news=(
                            NewsItem(
                                id="news-1",
                                title="Klassresan till Skansen",
                                content="Vi åker den 12 november.",
                                published_at="2026-10-26T12:00:00",
                            ),
                        ),
                        events=(
                            SchoolEvent(
                                id="event-1",
                                title="Föräldramöte",
                                description="I klassrummet",
                                start=event_start,
                                end=event_end,
                                all_day=False,
                            ),
                        ),
                    )
                )
            return result

        async def async_close(self):
            return None

    import custom_components.infomentor as integration
    from custom_components.infomentor import config_flow

    monkeypatch.setattr(integration, "InfoMentorApi", FakeApi)
    monkeypatch.setattr(config_flow, "InfoMentorApi", FakeApi)
    return FakeApi
