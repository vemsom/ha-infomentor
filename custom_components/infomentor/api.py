"""Async, read-only client for InfoMentor Hub.

Authentication mirrors the verified mobile app grant used by the
``infomentor-mcp`` project:

1. ``POST im.infomentor.se/Authentication/OAuth2/Token`` (form encoded,
   ``grant_type=refresh_token``) exchanges the stored refresh token for an
   access token (600 s lifetime) and a possibly rotated refresh token.
2. ``GET api-im.infomentor.se/NA1/Authentication/sso`` with that bearer token
   returns a web sign-in URL.
3. The SAML form chain on that URL is followed so the shared aiohttp cookie jar
   receives a Hub session; data is then read with those cookies from
   ``hub.infomentor.se``.

The bearer token is never sent to the web sign-in chain, and no token or
personal data is ever included in a log record or an exception message.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    API_ORIGIN,
    AUTH_ORIGIN,
    CLIENT_ID,
    CLIENT_SECRET,
    HUB_ORIGIN,
    MAX_MESSAGE_BODIES,
    MAX_PUPIL_SWITCH_HOPS,
    MAX_SSO_HOPS,
    PATH_CALENDAR,
    PATH_CHILDCARE,
    PATH_MESSAGE,
    PATH_NEWS,
    PATH_NOTIFICATIONS,
    PATH_SWITCH_PUPIL,
    REQUEST_TIMEOUT,
    SCOPE,
    SSO_PATH,
    TIME_ZONE,
    TOKEN_PATH,
    USER_AGENT,
)

TIME_ZONE_INFO = ZoneInfo(TIME_ZONE)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
HUB_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml"}

ALLOWED_HOSTS = frozenset(
    {
        "infomentor.se",
        "hub.infomentor.se",
        "sso.infomentor.se",
        "im.infomentor.se",
        "api-im.infomentor.se",
    }
)
ALLOWED_SUFFIXES = (".infomentor.se", ".stockholm.se")

_PUPIL_SWITCH_PATH = re.compile(
    r"/Account/PupilSwitcher/SwitchPupil/([^/?#'\"\\]+)", re.IGNORECASE
)
_SELECTED_PUPIL_NAME = re.compile(
    r"selectedPupilName\s*:\s*'((?:\\.|[^'])*)'", re.IGNORECASE
)

TokenCallback = Callable[[str], None]


class InfoMentorError(Exception):
    """Base error for this integration; messages never contain secrets."""


class InfoMentorAuthError(InfoMentorError):
    """The refresh token was rejected; the user must supply a new one."""


class InfoMentorConnectionError(InfoMentorError):
    """InfoMentor could not be reached or answered with an unexpected status."""


class InfoMentorResponseError(InfoMentorError):
    """InfoMentor answered, but not in the documented shape."""


class _SessionExpired(InfoMentorError):
    """Internal: the Hub cookie session needs to be re-established."""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Child:
    """A linked pupil as exposed by the Hub pupil switcher."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Notification:
    """One Hub notification record for the selected pupil."""

    id: str
    title: str | None
    state: str | None
    sent_at: str | None
    notification_type: str | None
    source_reference: str | None = None
    subject: str | None = None
    body: str | None = None

    @property
    def is_unread(self) -> bool:
        return (self.state or "").strip().lower() == "new"


@dataclass(frozen=True, slots=True)
class NewsItem:
    """One school news item."""

    id: str
    title: str | None
    content: str | None
    published_at: str | None


@dataclass(frozen=True, slots=True)
class SchoolEvent:
    """One calendar entry, interpreted in Europe/Stockholm."""

    id: str
    title: str
    description: str | None
    start: datetime
    end: datetime
    all_day: bool


@dataclass(frozen=True, slots=True)
class ChildcareDay:
    """A submitted before/after-school care day (optional data)."""

    day: date
    starts_at: datetime | None
    ends_at: datetime | None
    is_closed: bool
    on_leave: bool


@dataclass(frozen=True, slots=True)
class ChildData:
    """Everything the coordinator holds for one child."""

    child: Child
    notifications: tuple[Notification, ...] = ()
    news: tuple[NewsItem, ...] = ()
    events: tuple[SchoolEvent, ...] = ()
    childcare: tuple[ChildcareDay, ...] = ()

    @property
    def unread_count(self) -> int:
        return sum(1 for item in self.notifications if item.is_unread)

    @property
    def latest_news(self) -> NewsItem | None:
        return self.news[0] if self.news else None

    @property
    def next_event(self) -> SchoolEvent | None:
        now = datetime.now(TIME_ZONE_INFO)
        upcoming = [event for event in self.events if event.end >= now]
        return upcoming[0] if upcoming else None


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HtmlForm:
    """A form found in a sign-in page; ``action`` may be relative."""

    action: str = field(repr=False)
    fields: dict[str, str] = field(repr=False, default_factory=dict)


class _FormParser(HTMLParser):
    """Collect ``<form>`` actions and their ``<input>`` values."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[HtmlForm] = []
        self._action: str | None = None
        self._fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._action = attributes.get("action") or ""
            self._fields = {}
        elif tag == "input" and self._action is not None:
            name = attributes.get("name")
            if name:
                self._fields[name] = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._action is not None:
            self.forms.append(HtmlForm(self._action, dict(self._fields)))
            self._action = None
            self._fields = {}


def parse_forms(html: str) -> list[HtmlForm]:
    """Return every form in a sign-in page, in document order."""
    parser = _FormParser()
    parser.feed(html)
    parser.close()
    return parser.forms


def find_saml_form(html: str) -> HtmlForm | None:
    """Return the next form in a SAML POST binding chain, if any."""
    forms = parse_forms(html)
    for form in forms:
        if "SAMLResponse" in form.fields or "SAMLRequest" in form.fields:
            return form
    if "login in progress" in html.lower() and forms:
        return forms[0]
    return None


def parse_child_ids(page: str) -> list[str]:
    """Extract linked pupil IDs from the Hub page's switcher routes."""
    return list(
        dict.fromkeys(match.group(1) for match in _PUPIL_SWITCH_PATH.finditer(page))
    )


def parse_selected_name(page: str) -> str | None:
    """Extract the currently selected pupil's display name from a Hub page."""
    match = _SELECTED_PUPIL_NAME.search(page)
    if match is None:
        return None
    name = match.group(1).replace("\\'", "'").replace("\\\\", "\\")
    name = name.strip()
    return name or None


def same_name(first: str, second: str | None) -> bool:
    """Compare pupil display names loosely (order of names may differ)."""
    if second is None:
        return False
    normalize = lambda value: " ".join(value.lower().replace(",", " ").split())
    return normalize(first) == normalize(second)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def stockholm_datetime(value: object) -> datetime:
    """Interpret Hub wall-clock timestamps in Stockholm, keeping aware instants."""
    if not isinstance(value, str):
        raise InfoMentorResponseError("InfoMentor returned a non-string timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise InfoMentorResponseError(
            "InfoMentor returned an invalid timestamp"
        ) from err
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TIME_ZONE_INFO)
    return parsed.astimezone(TIME_ZONE_INFO)


def _attach_news_text(
    notifications: tuple[Notification, ...], news: tuple[NewsItem, ...]
) -> tuple[Notification, ...]:
    """Give a news notification the same subject/body shape as a message."""
    by_id = {item.id: item for item in news}
    enriched: list[Notification] = []
    for item in notifications:
        match = re.search(r"news/(\d+)", item.source_reference or "")
        article = by_id.get(match.group(1)) if match else None
        if article is not None:
            text = re.sub(r"<[^>]+>", " ", article.content or "")
            text = " ".join(text.split())
            enriched.append(
                replace(item, subject=article.title, body=text or None)
            )
        else:
            enriched.append(item)
    return tuple(enriched)


def parse_notifications(payload: object) -> tuple[Notification, ...]:
    """Parse the notifications endpoint for the currently selected pupil."""
    if not isinstance(payload, dict) or not isinstance(
        payload.get("notifications"), list
    ):
        raise InfoMentorResponseError("Notification response had no notifications list")
    items: dict[str, Notification] = {}
    for row in payload["notifications"]:
        if not isinstance(row, dict):
            raise InfoMentorResponseError("Notification record was not an object")
        if not isinstance(row.get("currentlySelectedPupil"), bool):
            raise InfoMentorResponseError("Notification record lacked the pupil marker")
        if not row["currentlySelectedPupil"]:
            continue
        identifier = row.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
            raise InfoMentorResponseError("Notification record lacked an id")
        item_id = str(identifier)
        items[item_id] = Notification(
            id=item_id,
            title=_string_or_none(row.get("title")),
            state=_string_or_none(row.get("state")),
            sent_at=_string_or_none(row.get("dateSent")),
            notification_type=_string_or_none(row.get("type")),
            source_reference=_string_or_none(row.get("url")),
        )
    ordered = sorted(items.values(), key=lambda item: item.sent_at or "", reverse=True)
    return tuple(ordered)


def parse_news(payload: object, limit: int) -> tuple[NewsItem, ...]:
    """Parse and truncate the news list, newest first."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise InfoMentorResponseError("News response had no items list")
    items: dict[str, NewsItem] = {}
    for row in payload["items"]:
        if not isinstance(row, dict) or row.get("id") is None:
            raise InfoMentorResponseError("News item lacked an id")
        item_id = str(row["id"])
        items.setdefault(
            item_id,
            NewsItem(
                id=item_id,
                title=_string_or_none(row.get("title")),
                content=_string_or_none(row.get("content")),
                published_at=_string_or_none(row.get("publishedDate")),
            ),
        )
    ordered = sorted(
        items.values(), key=lambda item: item.published_at or "", reverse=True
    )
    return tuple(ordered[: max(limit, 0)])


def parse_calendar(
    rows: object, start_date: date, end_date: date
) -> tuple[SchoolEvent, ...]:
    """Parse calendar entries overlapping ``start_date``..``end_date``."""
    if not isinstance(rows, list):
        raise InfoMentorResponseError("Calendar response was not an event list")
    events: dict[str, SchoolEvent] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise InfoMentorResponseError("Calendar entry was not an object")
        identifier = row.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
            raise InfoMentorResponseError("Calendar entry lacked an id")
        all_day = row.get("isAllDayEvent")
        if not isinstance(all_day, bool):
            raise InfoMentorResponseError("Calendar entry lacked the all-day flag")
        start = stockholm_datetime(row.get("startDateFull"))
        end = stockholm_datetime(row.get("endDateFull"))
        if end < start:
            raise InfoMentorResponseError("Calendar entry ended before it started")
        if start.date() > end_date or end.date() < start_date:
            continue
        event_id = str(identifier)
        events.setdefault(
            event_id,
            SchoolEvent(
                id=event_id,
                title=_string_or_none(row.get("title")) or "Skolhändelse",
                description=_string_or_none(row.get("text"))
                or _string_or_none(row.get("description")),
                start=start,
                end=end,
                all_day=all_day,
            ),
        )
    return tuple(sorted(events.values(), key=lambda item: (item.start, item.id)))


def parse_childcare(
    payload: object, start_date: date, end_date: date
) -> tuple[ChildcareDay, ...]:
    """Parse one week of submitted childcare registrations."""
    if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
        raise InfoMentorResponseError("Childcare response had no days list")
    days: list[ChildcareDay] = []
    for row in payload["days"]:
        if not isinstance(row, dict):
            raise InfoMentorResponseError("Childcare day was not an object")
        on_leave = row.get("onLeave")
        is_closed = row.get("isSchoolClosed")
        if not isinstance(on_leave, bool) or not isinstance(is_closed, bool):
            raise InfoMentorResponseError("Childcare day lacked its flags")
        day = stockholm_datetime(row.get("date")).date()
        if not start_date <= day <= end_date:
            continue
        times: dict[str, datetime | None] = {}
        for source, target in (
            ("startDateTime", "starts_at"),
            ("endDateTime", "ends_at"),
        ):
            value = row.get(source)
            times[target] = stockholm_datetime(value) if value else None
        days.append(
            ChildcareDay(
                day=day,
                starts_at=times["starts_at"],
                ends_at=times["ends_at"],
                is_closed=is_closed,
                on_leave=on_leave,
            )
        )
    return tuple(sorted(days, key=lambda item: item.day))


def child_scope_hash(child_ids: list[str]) -> str:
    """Stable, non-reversible fingerprint of the linked pupils.

    Used as config entry unique id so a re-added entry is recognised without
    storing a token or a name in the registry.
    """
    digest = hashlib.sha256("\n".join(sorted(child_ids)).encode("utf-8"))
    return digest.hexdigest()[:16]


def validate_url(url: str, base: str | None = None) -> str:
    """Resolve and restrict requests to InfoMentor/Stockholm HTTPS hosts."""
    resolved = urljoin(base, url) if base else url
    parsed = urlsplit(resolved)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES)
    ):
        raise InfoMentorResponseError(
            f"InfoMentor sign-in pointed outside the approved hosts ({host or 'missing'})"
        )
    return resolved


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Response:
    """A fully read HTTP response; no connection is left open."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError as err:
            raise InfoMentorResponseError("InfoMentor did not return JSON") from err

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class InfoMentorApi:
    """Read-only InfoMentor Hub client bound to one refresh token."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        *,
        on_token_refresh: TokenCallback | None = None,
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._on_token_refresh = on_token_refresh
        self._access_token: str | None = None
        self._access_expires_at: float = 0.0
        self._hub_ready = False
        self._child_ids: set[str] = set()
        self._child_names: dict[str, str] = {}
        self._selected_child_id: str | None = None
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str:
        """The current refresh token; may have rotated since construction."""
        return self._refresh_token

    async def async_validate(self) -> list[Child]:
        """Prove the token works: refresh, open a Hub session, read pupils + news."""
        children = await self.async_get_children(with_names=True)
        if not children:
            raise InfoMentorResponseError("InfoMentor listed no linked pupils")
        await self._async_with_renewal(self._async_news)
        return children

    async def async_close(self) -> None:
        """Release per-account state. The aiohttp session is owned by Home Assistant."""
        self._access_token = None
        self._hub_ready = False
        self._child_ids = set()
        self._child_names = {}

    # -- public reads ------------------------------------------------------- #

    async def async_get_children(self, *, with_names: bool) -> list[Child]:
        """List linked pupils, resolving display names when asked to."""
        async with self._lock:
            return await self._async_with_renewal(
                lambda: self._async_get_children(with_names)
            )

    async def async_fetch(
        self,
        child_ids: list[str],
        *,
        news_limit: int,
        calendar_start: date,
        calendar_end: date,
        include_childcare: bool = False,
        childcare_end: date | None = None,
    ) -> list[ChildData]:
        """Read all entities' data for the requested children."""
        async with self._lock:
            await self.async_ensure_session()
            if not self._child_ids:
                await self._async_with_renewal(self._async_refresh_child_ids)
            missing = [
                child_id for child_id in child_ids if child_id not in self._child_ids
            ]
            if missing:
                raise InfoMentorResponseError(
                    "A configured pupil is no longer linked to this InfoMentor account"
                )
            results: list[ChildData] = []
            for child_id in child_ids:
                results.append(
                    await self._async_with_renewal(
                        lambda child_id=child_id: self._async_fetch_child(
                            child_id,
                            news_limit=news_limit,
                            calendar_start=calendar_start,
                            calendar_end=calendar_end,
                            include_childcare=include_childcare,
                            childcare_end=childcare_end or calendar_end,
                        )
                    )
                )
            return results

    # -- session handling --------------------------------------------------- #

    async def async_ensure_session(self) -> None:
        """Make sure a Hub cookie session exists, renewing it when needed."""
        if self._hub_ready:
            return
        refreshed = await self._async_refresh_access_token_if_needed()
        try:
            await self._async_sso()
        except InfoMentorAuthError:
            if refreshed:
                raise
            # A cached access token may have expired early: one refresh, one retry.
            await self._async_token_request()
            await self._async_sso()
        self._hub_ready = True

    async def _async_refresh_access_token_if_needed(self) -> bool:
        if (
            self._access_token is not None
            and time.monotonic() < self._access_expires_at
        ):
            return False
        await self._async_token_request()
        return True

    async def _async_token_request(self) -> None:
        """Exchange the refresh token; persist a rotated token immediately."""
        response = await self._async_request(
            "POST",
            AUTH_ORIGIN + TOKEN_PATH,
            data={
                "refresh_token": self._refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "scope": SCOPE,
            },
            allow_redirects=False,
        )
        if response.status in (400, 401):
            raise InfoMentorAuthError(
                "InfoMentor rejected the stored refresh token; a new one is required"
            )
        if response.status != 200:
            raise InfoMentorConnectionError(
                f"InfoMentor token endpoint returned HTTP {response.status}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise InfoMentorResponseError("InfoMentor token response was not an object")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise InfoMentorResponseError(
                "InfoMentor token response had no refresh token"
            )
        # The old token stops working as soon as a new one is issued. Keep the
        # new one before anything else in the answer can fail.
        if refresh_token != self._refresh_token:
            self._refresh_token = refresh_token
            if self._on_token_refresh is not None:
                self._on_token_refresh(refresh_token)
        if not isinstance(access_token, str) or not access_token:
            raise InfoMentorResponseError(
                "InfoMentor token response had no access token"
            )
        expires_in = payload.get("expires_in", 600)
        try:
            lifetime = int(expires_in)
        except (TypeError, ValueError):
            lifetime = 600
        # Refresh slightly before the advertised expiry to avoid a wasted round trip.
        self._access_expires_at = time.monotonic() + max(lifetime - 30, 0)
        self._access_token = access_token
        self._hub_ready = False

    async def _async_sso(self) -> None:
        """Bridge the bearer token into a Hub cookie session."""
        response = await self._async_request(
            "GET",
            API_ORIGIN + SSO_PATH,
            headers={"Authorization": f"Bearer {self._access_token}"},
            allow_redirects=False,
        )
        if response.status == 401:
            raise InfoMentorAuthError(
                "InfoMentor rejected the access token at the SSO bridge"
            )
        if response.status != 200:
            raise InfoMentorConnectionError(
                f"InfoMentor SSO bridge returned HTTP {response.status}"
            )
        try:
            url = response.json()
        except InfoMentorResponseError:
            url = response.text().strip()
        if not isinstance(url, str) or not url:
            raise InfoMentorResponseError(
                "InfoMentor SSO bridge returned no sign-in URL"
            )
        await self._async_follow_sign_in(validate_url(url))

    async def _async_follow_sign_in(self, url: str) -> None:
        """Follow the SAML redirect/form chain to collect Hub cookies."""
        response = await self._async_request(
            "GET", url, headers=HTML_HEADERS, allow_redirects=False
        )
        for _ in range(MAX_SSO_HOPS):
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise InfoMentorResponseError(
                        "InfoMentor sign-in redirect had no destination"
                    )
                response = await self._async_request(
                    "GET",
                    validate_url(location, response.url),
                    headers=HTML_HEADERS,
                    allow_redirects=False,
                )
                continue
            if response.status >= 400:
                raise InfoMentorConnectionError(
                    f"InfoMentor sign-in returned HTTP {response.status}"
                )
            form = find_saml_form(response.text())
            if form is None:
                return
            response = await self._async_request(
                "POST",
                validate_url(form.action, response.url),
                data=form.fields,
                headers=HTML_HEADERS,
                allow_redirects=False,
            )
        raise InfoMentorResponseError("InfoMentor sign-in chain exceeded its hop limit")

    async def _async_with_renewal(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        """Run one read; on an expired Hub session renew once and replay once.

        A rejected token or a refused sign-in raises InfoMentorAuthError from
        the renewal itself. When the renewal succeeds and the Hub still answers
        with a redirect or an empty page, the token is fine and the Hub is not:
        a maintenance page, for example. That is a temporary error, so the
        next poll tries again instead of asking for BankID.
        """
        try:
            return await operation()
        except _SessionExpired:
            self._hub_ready = False
            await self.async_ensure_session()
            try:
                return await operation()
            except _SessionExpired as err:
                self._hub_ready = False
                raise InfoMentorConnectionError(
                    "InfoMentor Hub did not accept a fresh session; retrying later"
                ) from err

    # -- reads -------------------------------------------------------------- #

    async def _async_refresh_child_ids(self) -> None:
        page = await self._async_hub_page("/")
        child_ids = parse_child_ids(page)
        if not child_ids:
            raise InfoMentorResponseError("InfoMentor Hub listed no linked pupils")
        self._child_ids = set(child_ids)

    async def _async_get_children(self, with_names: bool) -> list[Child]:
        await self.async_ensure_session()
        page = await self._async_hub_page("/")
        child_ids = parse_child_ids(page)
        if not child_ids:
            raise InfoMentorResponseError("InfoMentor Hub listed no linked pupils")
        self._child_ids = set(child_ids)
        if not with_names:
            return [
                Child(id=child_id, name=self._child_names.get(child_id, ""))
                for child_id in child_ids
            ]

        initial_name = parse_selected_name(page)
        children: list[Child] = []
        for child_id in child_ids:
            page = await self._async_select_pupil(child_id)
            name = parse_selected_name(page)
            if name is None:
                raise InfoMentorResponseError(
                    "InfoMentor Hub did not show the selected pupil's name"
                )
            if any(same_name(name, child.name) for child in children):
                raise InfoMentorResponseError(
                    "InfoMentor pupil selection was ambiguous; no data read"
                )
            if initial_name is not None and same_name(name, initial_name):
                self._selected_child_id = child_id
            children.append(Child(id=child_id, name=name))
            self._child_names[child_id] = name
        if self._selected_child_id in self._child_ids:
            await self._async_select_pupil(self._selected_child_id)
        return children

    async def _async_select_pupil(self, child_id: str) -> str:
        """Switch the Hub's selected pupil, refusing to leave the Hub host."""
        if child_id not in self._child_ids:
            raise InfoMentorResponseError("The pupil is not linked to this account")
        url = f"{HUB_ORIGIN}{PATH_SWITCH_PUPIL}{quote(child_id, safe='')}"
        for _ in range(MAX_PUPIL_SWITCH_HOPS):
            response = await self._async_request(
                "GET", url, headers=HTML_HEADERS, allow_redirects=False
            )
            if response.status == 403:
                raise InfoMentorError("InfoMentor denied pupil switching (HTTP 403)")
            if response.status in (401,):
                raise _SessionExpired("Hub session expired during pupil switch")
            if response.status not in REDIRECT_STATUSES:
                if response.status >= 400:
                    raise InfoMentorConnectionError(
                        f"InfoMentor pupil switch returned HTTP {response.status}"
                    )
                if not response.body.strip():
                    raise InfoMentorResponseError(
                        "InfoMentor returned an empty page after switching pupil"
                    )
                self._selected_child_id = child_id
                return response.text()
            location = response.headers.get("Location")
            if not location:
                raise InfoMentorResponseError(
                    "Pupil switch redirect had no destination"
                )
            url = validate_url(location, response.url)
            parsed = urlsplit(url)
            if (
                parsed.netloc != urlsplit(HUB_ORIGIN).netloc
                or "login" in parsed.path.lower()
            ):
                raise _SessionExpired("Hub session expired during pupil switch")
        raise InfoMentorResponseError("Pupil switch exceeded its redirect limit")

    async def _async_fetch_child(
        self,
        child_id: str,
        *,
        news_limit: int,
        calendar_start: date,
        calendar_end: date,
        include_childcare: bool,
        childcare_end: date,
    ) -> ChildData:
        await self._async_select_pupil(child_id)
        notifications = await self._async_notifications()
        notifications = await self._async_message_bodies(notifications)
        news = await self._async_news(news_limit)
        notifications = _attach_news_text(notifications, news)
        events = await self._async_calendar(calendar_start, calendar_end)
        childcare: tuple[ChildcareDay, ...] = ()
        if include_childcare:
            childcare = await self._async_childcare(calendar_start, childcare_end)
        return ChildData(
            child=Child(id=child_id, name=self._child_names.get(child_id, "")),
            notifications=notifications,
            news=news,
            events=events,
            childcare=childcare,
        )

    async def _async_hub_post(self, path: str, payload: object) -> Any:
        if not self._hub_ready:
            await self.async_ensure_session()
        response = await self._async_request(
            "POST",
            HUB_ORIGIN + path,
            json=payload,
            headers=HUB_HEADERS,
            allow_redirects=False,
        )
        if response.status == 403:
            raise InfoMentorError("InfoMentor denied access (HTTP 403); not retried")
        if response.status in (401,) or response.status in REDIRECT_STATUSES:
            raise _SessionExpired("Hub session expired")
        if response.status != 200:
            raise InfoMentorConnectionError(
                f"InfoMentor Hub returned HTTP {response.status}"
            )
        if not response.body.strip():
            raise _SessionExpired("Hub returned an empty successful response")
        return response.json()

    async def _async_hub_page(self, path: str) -> str:
        if not self._hub_ready:
            await self.async_ensure_session()
        response = await self._async_request(
            "GET", f"{HUB_ORIGIN}{path}", headers=HTML_HEADERS, allow_redirects=False
        )
        if response.status == 403:
            raise InfoMentorError(
                "InfoMentor denied Hub access (HTTP 403); not retried"
            )
        if response.status == 401 or response.status in REDIRECT_STATUSES:
            raise _SessionExpired("Hub session expired")
        if response.status != 200:
            raise InfoMentorConnectionError(
                f"InfoMentor Hub returned HTTP {response.status}"
            )
        if not response.body.strip():
            raise _SessionExpired("Hub returned an empty successful response")
        return response.text()

    async def _async_notifications(self) -> tuple[Notification, ...]:
        return parse_notifications(await self._async_hub_post(PATH_NOTIFICATIONS, {}))

    async def _async_message_body(
        self, message_id: str
    ) -> tuple[str | None, str | None]:
        """Subject and plain-text body of one message.

        The Hub marks the message read when its detail is opened, so the
        pupil's unread count drops afterwards. Accepted by the owner.
        """
        payload = await self._async_hub_post(f"{PATH_MESSAGE}{message_id}", {})
        if not isinstance(payload, dict):
            return None, None
        return (
            _string_or_none(payload.get("messageSubject")),
            _string_or_none(
                payload.get("messageBodyPlainText") or payload.get("messageBody")
            ),
        )

    async def _async_message_bodies(
        self, notifications: tuple[Notification, ...]
    ) -> tuple[Notification, ...]:
        """Attach message bodies: the newest message plus unread ones (bounded).

        The newest message is always read so the sensor carries its text even
        after it has been read elsewhere; unread ones follow so an alert can
        show what arrived.
        """
        def message_id(item: Notification) -> str | None:
            if (item.notification_type or "") != "MessageCreated":
                return None
            match = re.search(r"message/show/(\d+)", item.source_reference or "")
            return match.group(1) if match else None

        wanted: list[tuple[str, str]] = []
        newest = next(
            (item for item in notifications if message_id(item) is not None), None
        )
        if newest is not None:
            wanted.append((newest.id, message_id(newest) or ""))
        for item in notifications:
            if not item.is_unread:
                continue
            identifier = message_id(item)
            if identifier and all(entry[0] != item.id for entry in wanted):
                wanted.append((item.id, identifier))
            if len(wanted) >= MAX_MESSAGE_BODIES:
                break
        if not wanted:
            return notifications
        details: dict[str, tuple[str | None, str | None]] = {}
        for notification_id, msg_id in wanted:
            try:
                details[notification_id] = await self._async_message_body(msg_id)
            except InfoMentorError:
                details[notification_id] = (None, None)
        return tuple(
            replace(item, subject=details[item.id][0], body=details[item.id][1])
            if item.id in details
            else item
            for item in notifications
        )

    async def _async_news(self, limit: int = 10) -> tuple[NewsItem, ...]:
        return parse_news(await self._async_hub_post(PATH_NEWS, {}), limit)

    async def _async_calendar(
        self, start_date: date, end_date: date
    ) -> tuple[SchoolEvent, ...]:
        payload = await self._async_hub_post(
            PATH_CALENDAR,
            {"startDate": start_date.isoformat(), "endDate": end_date.isoformat()},
        )
        return parse_calendar(payload, start_date, end_date)

    async def _async_childcare(
        self, start_date: date, end_date: date
    ) -> tuple[ChildcareDay, ...]:
        week = start_date - timedelta(days=start_date.weekday())
        days: list[ChildcareDay] = []
        while week <= end_date:
            payload = await self._async_hub_post(
                PATH_CHILDCARE,
                {
                    "date": f"{week.isoformat()}T00:00:00.000Z",
                    "showNextWeekIfNoMoreSchoolDays": False,
                },
            )
            days.extend(parse_childcare(payload, start_date, end_date))
            week += timedelta(days=7)
        return tuple(days)

    # -- transport ---------------------------------------------------------- #

    async def _async_request(self, method: str, url: str, **kwargs: Any) -> _Response:
        """Perform one request and read the whole body; no redirects by default."""
        kwargs.setdefault("allow_redirects", False)
        headers = {"User-Agent": USER_AGENT}
        headers.update(kwargs.pop("headers", None) or {})
        kwargs["headers"] = headers
        try:
            async with self._session.request(
                method,
                url,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                **kwargs,
            ) as response:
                body = await response.read()
                return _Response(
                    status=response.status,
                    headers=response.headers,
                    body=body,
                    url=str(response.url),
                )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise InfoMentorConnectionError(
                "Could not reach InfoMentor (network error or timeout)"
            ) from err
