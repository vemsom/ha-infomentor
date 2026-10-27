"""Setup, entity and reauth-trigger tests against a real Home Assistant core."""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
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
)

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "infomentor"


def _entry_data(children: list[dict[str, str]]) -> dict:
    return {CONF_REFRESH_TOKEN: "TOKEN", CONF_CHILDREN: children}


async def _setup_entry(hass, mock_info_mentor_api, children=None) -> MockConfigEntry:
    children = children or [{"id": "pupil-1", "name": "Ella"}]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="InfoMentor",
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
    assert manifest["version"] == "0.1.0"
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
    for step in ("user", "reauth", "bankid", "token"):
        assert step in strings["config"]["step"]
        assert "menu_options" in strings["config"]["step"][step] or step not in (
            "user",
            "reauth",
        )
    assert "bankid_scan" in strings["config"]["progress"]
