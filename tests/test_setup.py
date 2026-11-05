"""Setup, entity and reauth-trigger tests against a real Home Assistant core."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.infomentor.api import (
    InfoMentorAuthError,
    InfoMentorConnectionError,
)
from custom_components.infomentor.const import (
    CONF_CHILDREN,
    CONF_INCLUDE_CHILDCARE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    MAX_NEWS_CONTENT_LENGTH,
    MAX_NOTIFICATION_BODY_LENGTH,
)
from custom_components.infomentor.sensor import (
    MAX_ATTRIBUTE_ITEMS,
    MAX_TITLE_LENGTH,
    _clip,
    _fit_notifications,
)

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "infomentor"


def _entry_data(children: list[dict[str, str]]) -> dict:
    return {CONF_REFRESH_TOKEN: "TOKEN", CONF_CHILDREN: children}


async def _setup_entry(
    hass, mock_info_mentor_api, children=None, title="InfoMentor"
) -> MockConfigEntry:
    children = children or [{"id": "pupil-1", "name": "Ella"}]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data=_entry_data(children),
        options={CONF_INCLUDE_CHILDCARE: False},
        unique_id="scope",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_creates_expected_entities(hass, mock_info_mentor_api) -> None:
    """The documented entity ids exist with the documented states."""
    entry = await _setup_entry(hass, mock_info_mentor_api)
    assert entry.state is ConfigEntryState.LOADED

    unread = hass.states.get("sensor.ella_olasta_skolhandelser")
    assert unread is not None
    assert unread.state == "1"
    assert unread.attributes["notifications"][0]["title"] == "Ny läxa i matematik"

    news = hass.states.get("sensor.ella_senaste_nyhet")
    assert news is not None
    assert news.state == "Klassresan till Skansen"
    assert news.attributes["published_at"] == "2026-10-26T12:00:00"
    assert news.attributes["content"]

    upcoming = hass.states.get("sensor.ella_nasta_handelse")
    assert upcoming is not None
    assert upcoming.state == "Föräldramöte"
    assert upcoming.attributes["title"] == "Föräldramöte"
    assert upcoming.attributes["all_day"] is False

    session = hass.states.get("sensor.ella_session")
    assert session is not None
    assert session.state == "ok"
    assert session.attributes["last_update"] is not None

    assert hass.states.get("calendar.ella_skola") is not None
    last_update = hass.states.get("sensor.infomentor_senaste_uppdatering")
    assert last_update is not None
    assert last_update.state not in ("unknown", "unavailable")

    assert session.attributes["include_childcare"] is False


async def test_calendar_returns_events(hass, mock_info_mentor_api) -> None:
    """The calendar exposes the fetched school events."""
    await _setup_entry(hass, mock_info_mentor_api)
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.ella_skola",
            "start_date_time": "2026-01-01T00:00:00+00:00",
            "end_date_time": "2030-12-31T00:00:00+00:00",
        },
        blocking=True,
        return_response=True,
    )
    events = response["calendar.ella_skola"]["events"]
    assert [event["summary"] for event in events] == ["Föräldramöte"]
    assert events[0]["start"]


async def test_setup_two_children(hass, mock_info_mentor_api) -> None:
    """Two configured children get two devices and two calendars."""
    await _setup_entry(
        hass,
        mock_info_mentor_api,
        [{"id": "pupil-1", "name": "Ella"}, {"id": "pupil-2", "name": "Olle"}],
    )
    assert hass.states.get("calendar.ella_skola") is not None
    assert hass.states.get("calendar.olle_skola") is not None
    assert hass.states.get("sensor.olle_olasta_skolhandelser") is not None


async def test_unload_entry(hass, mock_info_mentor_api) -> None:
    """Unloading unloads the entry; restored states are unavailable only."""
    entry = await _setup_entry(hass, mock_info_mentor_api)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("calendar.ella_skola").state == "unavailable"


async def test_auth_failure_triggers_reauth(hass, mock_info_mentor_api) -> None:
    """A rejected token starts the reauth flow instead of failing silently."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="InfoMentor",
        data=_entry_data([{"id": "pupil-1", "name": "Ella"}]),
        options={CONF_INCLUDE_CHILDCARE: False},
        unique_id="scope",
    )
    entry.add_to_hass(hass)
    mock_info_mentor_api.fetch_error = InfoMentorAuthError("rejected token")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [
        config_entries.SOURCE_REAUTH
    ]


async def test_connection_failure_retries(hass, mock_info_mentor_api) -> None:
    """A network failure marks the entry for retry, not for reauth."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="InfoMentor",
        data=_entry_data([{"id": "pupil-1", "name": "Ella"}]),
        options={CONF_INCLUDE_CHILDCARE: False},
        unique_id="scope",
    )
    entry.add_to_hass(hass)
    mock_info_mentor_api.fetch_error = InfoMentorConnectionError("offline")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


def test_manifest_and_translations_are_valid() -> None:
    """The manifest and both translation files are present and consistent."""
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text("utf-8"))
    assert manifest["domain"] == "infomentor"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["version"] == "0.1.10"
    assert manifest["requirements"] == []

    strings = json.loads((COMPONENT_DIR / "strings.json").read_text("utf-8"))
    swedish = json.loads(
        (COMPONENT_DIR / "translations" / "sv.json").read_text("utf-8")
    )

    # Every config/options step and error key exists in both files.
    for section in ("config", "options"):
        assert set(strings[section]) == set(swedish[section])
        assert set(strings[section].get("error", {})) == set(
            swedish[section].get("error", {})
        )
        for step, payload in strings[section]["step"].items():
            assert step in swedish[section]["step"]
            translated = swedish[section]["step"][step]
            assert set(payload.get("data", {})) == set(translated.get("data", {}))
            assert set(payload.get("menu_options", {})) == set(
                translated.get("menu_options", {})
            )
    # The BankID path is the default; its steps must be translated.
    for step in ("user", "reauth", "bankid", "bankid_phone", "token"):
        assert step in strings["config"]["step"]
        assert "menu_options" in strings["config"]["step"][step] or step not in (
            "user",
            "reauth",
        )
    assert "bankid_scan" in strings["config"]["progress"]


async def test_token_rotation_is_saved_without_a_reload(
    hass, mock_info_mentor_api
) -> None:
    """A rotated token is stored, and the running entry keeps polling.

    A reload would build a second client that rotates the token once more.
    """
    entry = await _setup_entry(hass, mock_info_mentor_api)
    clients = len(mock_info_mentor_api.instances)

    mock_info_mentor_api.instances[-1].on_token_refresh("ROTATED")
    await hass.async_block_till_done()

    assert entry.data[CONF_REFRESH_TOKEN] == "ROTATED"
    assert len(mock_info_mentor_api.instances) == clients
    assert entry.state is ConfigEntryState.LOADED


async def test_entry_has_a_session_of_its_own(hass, mock_info_mentor_api) -> None:
    """The Hub cookies do not live in Home Assistant's shared session."""
    await _setup_entry(hass, mock_info_mentor_api)
    assert mock_info_mentor_api.instances[-1].session is not async_get_clientsession(
        hass
    )


async def test_title_with_the_childrens_names_is_made_neutral(
    hass, mock_info_mentor_api
) -> None:
    """Home Assistant logs the title, so an old title with names is replaced."""
    entry = await _setup_entry(hass, mock_info_mentor_api, title="InfoMentor Ella")
    assert entry.title == "InfoMentor"


async def test_title_set_by_the_user_is_kept(hass, mock_info_mentor_api) -> None:
    entry = await _setup_entry(hass, mock_info_mentor_api, title="Skolan")
    assert entry.title == "Skolan"


async def test_device_of_a_deselected_child_is_removed(
    hass, mock_info_mentor_api
) -> None:
    """A child that is no longer selected leaves no device behind."""
    entry = await _setup_entry(
        hass,
        mock_info_mentor_api,
        [{"id": "pupil-1", "name": "Ella"}, {"id": "pupil-2", "name": "Olle"}],
    )
    registry = dr.async_get(hass)
    assert registry.async_get_device_by_identifier((DOMAIN, "pupil-2"), entry.entry_id) is not None

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}]}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get_device_by_identifier((DOMAIN, "pupil-2"), entry.entry_id) is None
    assert registry.async_get_device_by_identifier((DOMAIN, "pupil-1"), entry.entry_id) is not None
    assert hass.states.get("calendar.olle_skola") is None


def test_long_texts_are_cut() -> None:
    assert _clip(None, 10) is None
    assert _clip("kort", 10) == "kort"
    clipped = _clip("x" * 50, 10)
    assert clipped is not None and len(clipped) == 10 and clipped.endswith("…")


def _notification(body: str | None, title_length: int = 60) -> dict:
    letter = "ä"  # two bytes in UTF-8, the worst case for Swedish text
    return {
        "title": letter * title_length,
        "sent_at": "2026-10-27T07:00:00",
        "type": "Message",
        "subject": letter * title_length,
        "body": body,
    }


def test_notifications_always_fit_the_budget() -> None:
    """Even the longest possible titles and bodies fit the recorder's 16 KB."""
    items = [
        _notification("å" * MAX_NOTIFICATION_BODY_LENGTH, MAX_TITLE_LENGTH)
        for _ in range(MAX_ATTRIBUTE_ITEMS)
    ]
    fitted = _fit_notifications(items)
    assert len(json.dumps(fitted, ensure_ascii=False).encode()) <= 14000


def test_newest_message_keeps_its_text() -> None:
    """The oldest bodies give way first; the newest message keeps its body."""
    newest = "ö" * MAX_NOTIFICATION_BODY_LENGTH
    items = [_notification(newest)] + [
        _notification("å" * MAX_NOTIFICATION_BODY_LENGTH)
        for _ in range(MAX_ATTRIBUTE_ITEMS - 1)
    ]

    fitted = _fit_notifications(items)

    assert len(json.dumps(fitted, ensure_ascii=False).encode()) <= 14000
    assert fitted[0]["body"] == newest
    assert fitted[-1]["body"] is None


def test_short_notifications_are_left_alone() -> None:
    items = [_notification("Kort meddelande.") for _ in range(3)]
    assert [item["body"] for item in _fit_notifications(items)] == [
        "Kort meddelande."
    ] * 3


def test_news_content_fits_the_recorder_limit() -> None:
    news = {
        "published_at": "2026-10-26T12:00:00",
        "news_id": "1",
        "title": "ä" * MAX_TITLE_LENGTH,
        "content": "ä" * MAX_NEWS_CONTENT_LENGTH,
    }
    assert len(json.dumps(news, ensure_ascii=False).encode()) < 16384


async def test_calendar_window_is_half_open(hass, mock_info_mentor_api) -> None:
    """An event that ends exactly when the window starts is not in it."""
    await _setup_entry(hass, mock_info_mentor_api)
    tomorrow = dt_util.now() + timedelta(days=1)
    event_end = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)

    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.ella_skola",
            "start_date_time": event_end.isoformat(),
            "end_date_time": (event_end + timedelta(hours=2)).isoformat(),
        },
        blocking=True,
        return_response=True,
    )

    assert response["calendar.ella_skola"]["events"] == []
