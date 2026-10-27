"""Tests for the InfoMentor API client: parsing and the auth chain."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from urllib.parse import urljoin

import pytest
from aiohttp import web

from custom_components.infomentor import api as api_module
from custom_components.infomentor.api import (
    Child,
    InfoMentorApi,
    InfoMentorAuthError,
    InfoMentorResponseError,
    child_scope_hash,
    find_saml_form,
    parse_calendar,
    parse_child_ids,
    parse_childcare,
    parse_news,
    parse_notifications,
    parse_selected_name,
    stockholm_datetime,
    validate_url,
)

HUB_PAGE = """
<html><body>
<script>
var config = { selectedPupilName : 'Ella Testsson' };
</script>
<a href="/Account/PupilSwitcher/SwitchPupil/12345">Ella</a>
<a href="/Account/PupilSwitcher/SwitchPupil/67890">Olle</a>
</body></html>
"""

SIGN_IN_PAGE = """
<html><body>
<form method="post" action="/signin/consume">
  <input type="hidden" name="SAMLResponse" value="abc123"/>
  <input type="submit" value="Continue"/>
</form>
</body></html>
"""


def test_parse_child_ids_and_selected_name() -> None:
    assert parse_child_ids(HUB_PAGE) == ["12345", "67890"]
    assert parse_selected_name(HUB_PAGE) == "Ella Testsson"
    assert parse_child_ids("<html></html>") == []


def test_find_saml_form() -> None:
    form = find_saml_form(SIGN_IN_PAGE)
    assert form is not None
    assert form.action == "/signin/consume"
    assert form.fields["SAMLResponse"] == "abc123"
    assert find_saml_form("<html><body>done</body></html>") is None


def test_validate_url_allowlist() -> None:
    assert validate_url("https://hub.infomentor.se/") == "https://hub.infomentor.se/"
    assert (
        validate_url("/login", "https://sso.infomentor.se/x")
        == "https://sso.infomentor.se/login"
    )
    with pytest.raises(InfoMentorResponseError):
        validate_url("https://evil.example.com/")
    with pytest.raises(InfoMentorResponseError):
        validate_url("http://hub.infomentor.se/")
    with pytest.raises(InfoMentorResponseError):
        validate_url("https://hub.infomentor.se.evil.example/")
    # Stockholm SSO hosts are permitted.
    assert validate_url("https://login.stockholm.se/x").startswith("https://")


def test_parse_notifications_only_selected_pupil() -> None:
    payload = {
        "notifications": [
            {
                "id": 1,
                "title": "Ny läxa",
                "state": "New",
                "dateSent": "2026-10-27T07:00:00",
                "type": "Homework",
                "currentlySelectedPupil": True,
            },
            {
                "id": 2,
                "title": "Annan elev",
                "state": "New",
                "currentlySelectedPupil": False,
            },
            {
                "id": 3,
                "title": "Gammal",
                "state": "Seen",
                "dateSent": "2026-10-25T07:00:00",
                "currentlySelectedPupil": True,
            },
        ]
    }
    items = parse_notifications(payload)
    assert [item.id for item in items] == ["1", "3"]
    assert sum(1 for item in items if item.is_unread) == 1


def test_parse_notifications_rejects_unexpected_shape() -> None:
    with pytest.raises(InfoMentorResponseError):
        parse_notifications({"items": []})
    with pytest.raises(InfoMentorResponseError):
        parse_notifications({"notifications": [{"id": 1}]})


def test_parse_news_limit_and_order() -> None:
    payload = {
        "items": [
            {"id": 1, "title": "Äldre", "publishedDate": "2026-10-01T10:00:00"},
            {
                "id": 2,
                "title": "Nyare",
                "publishedDate": "2026-10-20T10:00:00",
                "content": "x",
            },
            {"id": 3, "title": "Nyast", "publishedDate": "2026-10-25T10:00:00"},
        ]
    }
    items = parse_news(payload, 2)
    assert [item.title for item in items] == ["Nyast", "Nyare"]
    with pytest.raises(InfoMentorResponseError):
        parse_news([], 10)


def test_parse_calendar_all_day_and_window() -> None:
    rows = [
        {
            "id": 10,
            "title": "Studiedag",
            "isAllDayEvent": True,
            "startDateFull": "2026-10-29T00:00:00",
            "endDateFull": "2026-10-29T23:59:59",
        },
        {
            "id": 11,
            "title": "Utanför fönstret",
            "isAllDayEvent": False,
            "startDateFull": "2026-12-01T08:00:00",
            "endDateFull": "2026-12-01T09:00:00",
        },
        {
            "id": 12,
            "title": "Föräldramöte",
            "text": "Klassrummet",
            "isAllDayEvent": False,
            "startDateFull": "2026-10-28T18:00:00",
            "endDateFull": "2026-10-28T19:00:00",
        },
    ]
    events = parse_calendar(rows, date(2026, 10, 27), date(2026, 11, 26))
    assert [event.title for event in events] == ["Föräldramöte", "Studiedag"]
    assert events[0].description == "Klassrummet"
    assert events[0].start.utcoffset() is not None
    assert events[1].all_day is True


def test_parse_childcare_flags() -> None:
    payload = {
        "days": [
            {
                "date": "2026-10-28T00:00:00",
                "onLeave": False,
                "isSchoolClosed": False,
                "startDateTime": "2026-10-28T07:00:00",
                "endDateTime": "2026-10-28T17:00:00",
            },
            {
                "date": "2026-10-29T00:00:00",
                "onLeave": True,
                "isSchoolClosed": False,
                "startDateTime": None,
                "endDateTime": None,
            },
        ]
    }
    days = parse_childcare(payload, date(2026, 10, 27), date(2026, 11, 2))
    assert len(days) == 2
    assert days[0].starts_at is not None
    assert days[1].on_leave is True


def test_child_scope_hash_is_stable() -> None:
    first = child_scope_hash(["b", "a"])
    second = child_scope_hash(["a", "b"])
    assert first == second
    assert first != child_scope_hash(["a"])
    assert len(first) == 16


def test_stockholm_timestamp_default_tz() -> None:
    parsed = stockholm_datetime("2026-10-28T08:15:00")
    assert parsed.utcoffset() is not None
    with pytest.raises(InfoMentorResponseError):
        stockholm_datetime(1234)


# --------------------------------------------------------------------------- #
# End-to-end auth chain against a local fake InfoMentor
# --------------------------------------------------------------------------- #


class FakeInfoMentor:
    """A minimal in-process imitation of the InfoMentor hosts."""

    def __init__(self) -> None:
        self.token_requests: list[dict[str, str]] = []
        self.bearer_headers: list[str | None] = []
        self.reject_refresh = False
        self.hub_posts: list[str] = []
        self.pupil_switches: list[str] = []

    def routes(self) -> list[web.RouteDef]:
        return [
            web.post("/Authentication/OAuth2/Token", self.token),
            web.get("/NA1/Authentication/sso", self.sso),
            web.get("/signin/step1", self.step1),
            web.post("/signin/consume", self.consume),
            web.get("/", self.hub_root),
            web.get("/Account/PupilSwitcher/SwitchPupil/{pupil}", self.switch),
            web.post("/Communication/News/GetNewsList", self.news),
            web.post(
                "/NotificationApp/NotificationApp/GetNotifications", self.notifications
            ),
            web.post("/calendarv2/calendarv2/getentries", self.calendar),
        ]

    async def token(self, request: web.Request) -> web.Response:
        form = {key: str(value) for key, value in (await request.post()).items()}
        self.token_requests.append(form)
        if self.reject_refresh or form.get("grant_type") != "refresh_token":
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response(
            {
                "access_token": "ACCESS",
                "refresh_token": "ROTATED",
                "expires_in": 600,
                "token_type": "Bearer",
            }
        )

    async def sso(self, request: web.Request) -> web.Response:
        self.bearer_headers.append(request.headers.get("Authorization"))
        return web.json_response(str(request.url.with_path("/signin/step1")))

    async def step1(self, request: web.Request) -> web.Response:
        return web.Response(text=SIGN_IN_PAGE, content_type="text/html")

    async def consume(self, request: web.Request) -> web.Response:
        await request.post()
        response = web.Response(status=302, headers={"Location": "/"})
        response.set_cookie("HUBSSO", "ok")
        return response

    async def hub_root(self, request: web.Request) -> web.Response:
        assert request.cookies.get("HUBSSO") == "ok"
        return web.Response(text=HUB_PAGE, content_type="text/html")

    async def switch(self, request: web.Request) -> web.Response:
        pupil = request.match_info["pupil"]
        self.pupil_switches.append(pupil)
        name = {"12345": "Ella Testsson", "67890": "Olle Testsson"}[pupil]
        return web.Response(
            text=f"<script>selectedPupilName : '{name}'</script>",
            content_type="text/html",
        )

    async def news(self, request: web.Request) -> web.Response:
        self.hub_posts.append("news")
        return web.json_response(
            {
                "items": [
                    {
                        "id": 1,
                        "title": "Klassresan",
                        "content": "12 november",
                        "publishedDate": "2026-10-26T12:00:00",
                    }
                ]
            }
        )

    async def notifications(self, request: web.Request) -> web.Response:
        self.hub_posts.append("notifications")
        return web.json_response(
            {
                "notifications": [
                    {
                        "id": 7,
                        "title": "Ny läxa",
                        "state": "New",
                        "dateSent": "2026-10-27T07:00:00",
                        "currentlySelectedPupil": True,
                    }
                ]
            }
        )

    async def calendar(self, request: web.Request) -> web.Response:
        self.hub_posts.append("calendar")
        return web.json_response(
            [
                {
                    "id": 3,
                    "title": "Föräldramöte",
                    "isAllDayEvent": False,
                    "startDateFull": "2026-10-28T18:00:00",
                    "endDateFull": "2026-10-28T19:00:00",
                }
            ]
        )


@pytest.fixture
async def fake_info_mentor(aiohttp_client, monkeypatch, socket_enabled):
    fake = FakeInfoMentor()
    app = web.Application()
    app.add_routes(fake.routes())
    client = await aiohttp_client(app)
    origin = str(client.make_url("")).rstrip("/")
    monkeypatch.setattr(api_module, "AUTH_ORIGIN", origin)
    monkeypatch.setattr(api_module, "API_ORIGIN", origin)
    monkeypatch.setattr(api_module, "HUB_ORIGIN", origin)
    monkeypatch.setattr(
        api_module, "ALLOWED_HOSTS", frozenset({"127.0.0.1", "localhost"})
    )
    # The fake runs over plain HTTP; the https allowlist has its own unit test.
    monkeypatch.setattr(
        api_module,
        "validate_url",
        lambda url, base=None: urljoin(base, url) if base else url,
    )
    return fake, client


async def test_auth_chain_and_reads(aiohttp_client, fake_info_mentor) -> None:
    fake, client = fake_info_mentor
    rotated: list[str] = []

    api = InfoMentorApi(client.session, "ORIGINAL", on_token_refresh=rotated.append)
    children = await api.async_validate()

    assert [child.name for child in children] == ["Ella Testsson", "Olle Testsson"]
    # Exact form of the documented refresh grant.
    assert fake.token_requests[0] == {
        "refresh_token": "ORIGINAL",
        "client_id": "notificationapp",
        "client_secret": "NONE",
        "grant_type": "refresh_token",
        "scope": "IM2-API-NOTIFICATION",
    }
    # Rotation is reported so the config entry can be updated.
    assert rotated == ["ROTATED"]
    assert api.refresh_token == "ROTATED"
    # The bearer token only went to the SSO bridge.
    assert fake.bearer_headers == ["Bearer ACCESS"]
    # Every hub call carried the cookie session.
    assert fake.hub_posts == ["news"]

    data = await api.async_fetch(
        ["12345"],
        news_limit=10,
        calendar_start=date(2026, 10, 27),
        calendar_end=date(2026, 11, 26),
    )
    assert len(data) == 1
    child = data[0]
    assert child.child.name == "Ella Testsson"
    assert child.unread_count == 1
    assert child.latest_news is not None and child.latest_news.title == "Klassresan"
    assert [event.title for event in child.events] == ["Föräldramöte"]
    assert child.childcare == ()
    assert "12345" in fake.pupil_switches


async def test_rejected_refresh_token_raises_auth_error(fake_info_mentor) -> None:
    fake, client = fake_info_mentor
    fake.reject_refresh = True
    api = InfoMentorApi(client.session, "STALE")
    with pytest.raises(InfoMentorAuthError):
        await api.async_validate()


async def test_hub_session_renewed_once(fake_info_mentor) -> None:
    fake, client = fake_info_mentor
    api = InfoMentorApi(client.session, "ORIGINAL")
    await api.async_validate()
    # Simulate the Hub dropping the cookie session between polls.
    api._hub_ready = False
    client.session.cookie_jar.clear()
    data = await api.async_fetch(
        ["67890"],
        news_limit=10,
        calendar_start=date(2026, 10, 27),
        calendar_end=date(2026, 11, 26),
    )
    assert data[0].child.name == "Olle Testsson"
    assert len(fake.token_requests) >= 1


def test_child_dataclass_is_immutable() -> None:
    child = Child(id="1", name="Ella")
    with pytest.raises(FrozenInstanceError):
        child.name = "Olle"  # type: ignore[misc]
