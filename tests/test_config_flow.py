"""Config, options and reauth flow tests."""

from __future__ import annotations

import asyncio
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.infomentor.api import (
    InfoMentorAuthError,
    InfoMentorConnectionError,
    child_scope_hash,
)
from custom_components.infomentor.const import (
    CONF_CHILDREN,
    CONF_INCLUDE_CHILDCARE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)


class FakeBankIdLogin:
    """Stands in for the BankID login; no network and no waiting."""

    outcome: ClassVar[str] = "ok"
    gate: ClassVar[asyncio.Event | None] = None
    instances: ClassVar[list[FakeBankIdLogin]] = []

    def __init__(self, session, *, method="qr", on_update=None, **_kwargs):
        self.session = session
        self.method = method
        self.on_update = on_update
        self.status = "starting"
        self.error: str | None = None
        self.qr_data = "bankid.FAKE.1759212345678.host"
        self.launch_url: str | None = None
        self.refresh_token: str | None = None
        FakeBankIdLogin.instances.append(self)

    async def async_start(self) -> None:
        if FakeBankIdLogin.outcome == "start_failure":
            raise InfoMentorConnectionError("offline")
        self.status = "waiting"
        if self.method == "same_device":
            self.launch_url = "https://app.bankid.com/?autostarttoken=T&redirect=null"
        if self.on_update is not None:
            self.on_update(self.qr_data, self.status, self.launch_url)

    async def async_wait(self) -> None:
        if FakeBankIdLogin.gate is not None:
            await FakeBankIdLogin.gate.wait()
        outcome = FakeBankIdLogin.outcome
        if outcome == "ok":
            self.status, self.refresh_token = "complete", "QRREFRESH"
        elif outcome == "timeout":
            self.status, self.error = "timeout", "timeout"
        elif outcome == "cancelled":
            self.status, self.error = "cancelled", "cancelled"
        elif outcome == "connection":
            self.status, self.error = "failed", "connection"
        else:
            self.status, self.error = "failed", "invalid_auth"
        if self.on_update is not None:
            self.on_update(None, self.status, self.launch_url)


@pytest.fixture
def mock_bankid_login(monkeypatch):
    """Replace the real BankID login in the config flow."""
    from custom_components.infomentor import config_flow

    FakeBankIdLogin.outcome = "ok"
    FakeBankIdLogin.gate = None
    FakeBankIdLogin.instances = []
    monkeypatch.setattr(config_flow, "BankIdLogin", FakeBankIdLogin)
    return FakeBankIdLogin


async def _open_menu(hass, source=config_entries.SOURCE_USER):
    """Start a flow and return its menu result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": source}
    )
    assert result["type"] is FlowResultType.MENU
    return result


async def _open_bankid(hass, option="bankid"):
    """Choose a BankID option and return the waiting dialog.

    Choosing the option starts the attempt and shows a progress step with the
    links in it; Home Assistant continues it by itself when the login is done.
    """
    result = await _open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS, result
    assert result["step_id"] == "bankid_wait"
    return result


async def _advance_progress(hass, flow_id):
    """Return the next result once the BankID task has finished."""
    await hass.async_block_till_done()
    if not hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        return None
    return await hass.config_entries.flow.async_configure(flow_id)


async def _finish_bankid(hass, flow_id):
    """Let the attempt end and return its outcome, with no Submit pressed."""
    return await _advance_progress(hass, flow_id)


# -- BankID (default path) -------------------------------------------------- #


async def test_menu_offers_bankid_first(hass, mock_info_mentor_api) -> None:
    """BankID is the default way in; the token is an advanced option."""
    result = await _open_menu(hass)

    assert result["step_id"] == "user"
    assert list(result["menu_options"]) == ["bankid", "bankid_phone", "token"]


async def test_bankid_flow_creates_entry(
    hass, hass_client_no_auth, mock_info_mentor_api, mock_bankid_login
) -> None:
    """Scanning the QR stores the fetched refresh token and nothing else."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    gate = asyncio.Event()
    mock_bankid_login.gate = gate
    result = await _open_bankid(hass)
    assert result["progress_action"] == "bankid_scan"
    qr_path = urlsplit(result["description_placeholders"]["qr_url"]).path

    # While the attempt runs the page is reachable and already shows a QR.
    client = await hass_client_no_auth()
    assert (await client.get(qr_path)).status == 200
    frame = await (await client.get(qr_path + "/frame")).json()
    assert frame["status"] in {"waiting", "verifying"}
    if frame["status"] == "waiting":
        assert frame["matrix"] is not None and len(frame["matrix"]) >= 21

    gate.set()
    result = await _finish_bankid(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_REFRESH_TOKEN: "QRREFRESH",
        CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}],
    }
    assert mock_bankid_login.instances[0].method == "qr"
    # The QR path is closed and forgotten as soon as the attempt is over.
    assert (await client.get(qr_path)).status == 410


async def test_bankid_flow_children_step(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """Two linked children still get the picker before the entry is created."""
    result = await _open_bankid(hass)
    result = await _finish_bankid(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "children"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHILDREN: ["pupil-2"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHILDREN] == [{"id": "pupil-2", "name": "Olle"}]


async def test_bankid_timeout_offers_a_new_qr(
    hass, hass_client_no_auth, mock_info_mentor_api, mock_bankid_login
) -> None:
    """A four-minute silence is reported as a timeout, with a fresh QR."""
    mock_bankid_login.outcome = "timeout"
    gate = asyncio.Event()
    mock_bankid_login.gate = gate
    first = await _open_bankid(hass)
    first_path = urlsplit(first["description_placeholders"]["qr_url"]).path

    gate.set()
    result = await _finish_bankid(hass, first["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bankid"
    assert result["errors"] == {"base": "bankid_timeout"}
    assert not hass.config_entries.async_entries(DOMAIN)

    client = await hass_client_no_auth()
    assert (await client.get(first_path)).status == 410

    # No order runs until Submit asks for one; then a fresh QR is offered.
    assert len(mock_bankid_login.instances) == 1
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert len(mock_bankid_login.instances) == 2
    second_path = urlsplit(result["description_placeholders"]["qr_url"]).path
    assert second_path != first_path
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()


async def test_bankid_cancelled_shows_an_error(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """A cancelled or rejected order never creates an entry."""
    mock_bankid_login.outcome = "cancelled"
    result = await _open_bankid(hass)
    result = await _finish_bankid(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "bankid_cancelled"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_bankid_failed_exchange_shows_an_error(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """A rejected code exchange keeps the dialog open and stores nothing."""
    mock_bankid_login.outcome = "invalid_auth"
    result = await _open_bankid(hass)
    result = await _finish_bankid(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_bankid_start_failure_aborts(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """Without a BankID order there is no QR to show, so the flow aborts."""
    mock_bankid_login.outcome = "start_failure"
    result = await _open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "bankid"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "bankid_unavailable"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_abandoned_flow_closes_the_qr_path(
    hass, hass_client_no_auth, mock_info_mentor_api, mock_bankid_login
) -> None:
    """Closing the dialog must not leave a working QR behind."""
    gate = asyncio.Event()
    mock_bankid_login.gate = gate
    result = await _open_bankid(hass)
    qr_path = urlsplit(result["description_placeholders"]["qr_url"]).path
    client = await hass_client_no_auth()
    assert (await client.get(qr_path)).status == 200

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert (await client.get(qr_path)).status == 410


async def test_bankid_rejects_a_duplicate_account(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """The same child scope cannot be added twice through BankID either."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    for _ in range(2):
        result = await _open_bankid(hass)
        result = await _finish_bankid(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bankid_flow_with_the_real_login_chain(
    hass, hass_client_no_auth, mock_info_mentor_api, fake_hosts, monkeypatch
) -> None:
    """The real login chain (fake hosts) drives the flow end to end."""
    fake, _client = fake_hosts
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]

    # The fake runs on 127.0.0.1; aiohttp only accepts IP cookies in an unsafe
    # jar. Home Assistant's default jar is stricter and the real hosts are
    # domain names, so this belongs to the test setup only.
    from custom_components.infomentor import config_flow

    monkeypatch.setattr(
        config_flow,
        "_new_bankid_session",
        lambda: aiohttp.ClientSession(
            cookie_jar=config_flow._new_bankid_cookie_jar(unsafe=True)
        ),
    )

    result = await _open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "bankid"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "bankid_wait"
    qr_path = urlsplit(result["description_placeholders"]["qr_url"]).path

    # The page already has a scannable QR when the dialog opens.
    client = await hass_client_no_auth()
    assert (await client.get(qr_path)).status == 200
    frame = await (await client.get(qr_path + "/frame")).json()
    assert frame["status"] in {"waiting", "verifying"}
    if frame["status"] == "waiting":
        assert frame["matrix"] is not None and len(frame["matrix"]) >= 21

    # No Submit: the dialog moves on by itself when the login is done.
    result = await _advance_progress(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_REFRESH_TOKEN: "REFRESH",
        CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}],
    }
    # The whole documented chain was walked, in order, exactly once.
    assert fake.initialize_calls == ["qr"]
    assert fake.token_requests[0]["grant_type"] == "authorization_code"
    assert fake.token_requests[0]["code"] == "CODE1"
    # The QR path dies with the flow.
    assert (await client.get(qr_path)).status == 410


# -- BankID on this phone (autostart link) ---------------------------------- #


async def test_bankid_phone_step_offers_the_autostart_link(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """The single-phone option hands the app link to the waiting dialog."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    gate = asyncio.Event()
    mock_bankid_login.gate = gate
    result = await _open_bankid(hass, option="bankid_phone")

    assert result["progress_action"] == "bankid_phone_scan"
    # The order is started in the same-device mode, not as a QR order.
    assert mock_bankid_login.instances[0].method == "same_device"
    placeholders = result["description_placeholders"]
    launch = urlsplit(placeholders["launch_url"])
    assert launch.scheme == "https" and launch.netloc == "app.bankid.com"
    assert parse_qs(launch.query) == {
        "autostarttoken": ["T"],
        "redirect": ["null"],
    }
    # The QR page stays reachable as the fallback the string links to.
    assert placeholders["qr_url"]

    gate.set()
    result = await _finish_bankid(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_REFRESH_TOKEN: "QRREFRESH",
        CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}],
    }


async def test_qr_step_publishes_no_autostart_link(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """The scanning path offers the QR page and no app link."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    gate = asyncio.Event()
    mock_bankid_login.gate = gate
    result = await _open_bankid(hass)

    assert result["progress_action"] == "bankid_scan"
    assert mock_bankid_login.instances[0].method == "qr"
    # An empty placeholder, never a dangling Markdown link.
    assert result["description_placeholders"]["launch_url"] == ""
    assert result["description_placeholders"]["qr_url"]

    gate.set()
    result = await _finish_bankid(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_bankid_phone_flow_with_the_real_login_chain(
    hass, mock_info_mentor_api, fake_hosts, monkeypatch
) -> None:
    """The same-device chain asks the SSO for an autostart token (initialize=url)."""
    fake, _client = fake_hosts
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]

    from custom_components.infomentor import config_flow

    monkeypatch.setattr(
        config_flow,
        "_new_bankid_session",
        lambda: aiohttp.ClientSession(
            cookie_jar=config_flow._new_bankid_cookie_jar(unsafe=True)
        ),
    )

    result = await _open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "bankid_phone"}
    )

    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "bankid_phone_scan"
    assert fake.initialize_calls == ["bankid"]
    launch = urlsplit(result["description_placeholders"]["launch_url"])
    assert parse_qs(launch.query)["autostarttoken"] == ["AUTOSTART"]

    result = await _finish_bankid(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_REFRESH_TOKEN: "REFRESH",
        CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}],
    }
    assert fake.token_requests[0]["code"] == "CODE1"


# -- refresh token (advanced) ----------------------------------------------- #
async def test_user_flow_single_child(hass, mock_info_mentor_api) -> None:
    """A one-child account skips the child picker."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]

    result = await _open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "token"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "TOKEN"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_REFRESH_TOKEN: "TOKEN",
        CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}],
    }
    assert result["title"] == "InfoMentor Ella"


async def test_user_flow_multiple_children(hass, mock_info_mentor_api) -> None:
    """The picker stores only the selected children."""
    result = await _open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "TOKEN"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "children"
    # The picker offers both linked children and accepts a subset.
    validated = result["data_schema"]({CONF_CHILDREN: ["pupil-2"]})
    assert validated == {CONF_CHILDREN: ["pupil-2"]}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHILDREN: ["pupil-2"]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHILDREN] == [{"id": "pupil-2", "name": "Olle"}]
    assert result["title"] == "InfoMentor Olle"


async def test_user_flow_invalid_auth(hass, mock_info_mentor_api) -> None:
    """A rejected token shows an error and creates nothing."""
    mock_info_mentor_api.validate_error = InfoMentorAuthError("rejected")

    result = await _open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "BROKEN"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_entry_aborts(hass, mock_info_mentor_api) -> None:
    """The same child scope cannot be added twice."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    result = await _open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "TOKEN"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    result = await _open_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "TOKEN2"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# -- reauth ----------------------------------------------------------------- #


def _reauth_entry(**overrides) -> MockConfigEntry:
    options = {
        "domain": DOMAIN,
        "title": "InfoMentor Ella",
        "data": {
            CONF_REFRESH_TOKEN: "OLD",
            CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}],
        },
        "unique_id": child_scope_hash(["pupil-1"]),
        "entry_id": "entry-id",
    }
    options.update(overrides)
    return MockConfigEntry(**options)


async def test_reauth_uses_bankid(
    hass, hass_client_no_auth, mock_info_mentor_api, mock_bankid_login
) -> None:
    """Reauth takes the same QR path and replaces the stored token."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    entry = _reauth_entry()
    entry.add_to_hass(hass)
    gate = asyncio.Event()
    mock_bankid_login.gate = gate

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reauth"
    assert list(result["menu_options"])[:2] == ["bankid", "bankid_phone"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "bankid"}
    )
    qr_path = urlsplit(result["description_placeholders"]["qr_url"]).path
    client = await hass_client_no_auth()
    assert (await client.get(qr_path)).status == 200

    gate.set()
    result = await _finish_bankid(hass, result["flow_id"])
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "QRREFRESH"
    assert (await client.get(qr_path)).status == 410


async def test_reauth_flow_updates_token(hass, mock_info_mentor_api) -> None:
    """The advanced token path still works for reauth."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    entry = _reauth_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    assert result["step_id"] == "token"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "NEW"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "NEW"


async def test_reauth_rejects_other_account(hass, mock_info_mentor_api) -> None:
    """A token for a different child scope must not replace the entry."""
    entry = _reauth_entry(unique_id="some-other-scope", entry_id="entry-id-2")
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "NEW"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}
    assert entry.data[CONF_REFRESH_TOKEN] == "OLD"


async def test_reauth_bankid_rejects_other_account(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """A BankID login for another account is refused the same way."""
    entry = _reauth_entry(unique_id="some-other-scope", entry_id="entry-id-3")
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "bankid"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _advance_progress(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}
    assert entry.data[CONF_REFRESH_TOKEN] == "OLD"


# -- options ---------------------------------------------------------------- #


async def test_options_flow_toggles_childcare(hass, mock_info_mentor_api) -> None:
    """The childcare schedule is off by default and opt-in through options."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="InfoMentor Ella",
        data={
            CONF_REFRESH_TOKEN: "TOKEN",
            CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}],
        },
        options={CONF_INCLUDE_CHILDCARE: False},
        unique_id="options-scope",
        entry_id="entry-options",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.include_childcare is False

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CHILDREN: ["pupil-1"], CONF_INCLUDE_CHILDCARE: True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_INCLUDE_CHILDCARE] is True
    assert entry.runtime_data.coordinator.include_childcare is True


def _siteminder_cookie_header(jar: aiohttp.CookieJar, value: str) -> str:
    """Store SMSESSION as login003 sets it; return what login001 is sent."""
    from http.cookies import SimpleCookie

    from yarl import URL

    cookie = SimpleCookie()
    cookie.load(f"SMSESSION={value}; Domain=.stockholm.se; Path=/; Secure")
    jar.update_cookies(
        cookie,
        URL("https://login003.stockholm.se/NECSadcmbid/authenticate/SiteMinderAuthADC"),
    )
    sent = jar.filter_cookies(
        URL("https://login001.stockholm.se/NECSadc/mbid/b64startpage.jsp")
    )
    return sent.output(header="", sep=";").strip()


async def test_bankid_session_sends_siteminder_cookie_unquoted() -> None:
    """SMSESSION must reach login001 exactly as SiteMinderAuthADC set it.

    The value is base64, so it holds ``/``, ``+`` and ``=``. aiohttp's default
    jar wraps such a value in double quotes, which SiteMinder rejects: the
    approved BankID login then falls back to the chooser page.
    """
    from custom_components.infomentor import config_flow

    value = "AAAAAQ/8Kc7+rTz9Qw/AbC0dEf1+Gh2IjK3lMn4=="

    # The control: the default jar is what broke the login.
    assert _siteminder_cookie_header(aiohttp.CookieJar(), value) == (
        f'SMSESSION="{value}"'
    )

    session = config_flow._new_bankid_session()
    try:
        header = _siteminder_cookie_header(session.cookie_jar, value)
    finally:
        await session.close()
    assert header == f"SMSESSION={value}"


async def test_qr_link_opens_in_a_tab_of_its_own(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """The QR page link carries target=_blank in the form and in the progress.

    A markdown link to Home Assistant's own address is routed inside the
    frontend and shows the dashboard instead of the QR page.
    """
    mock_bankid_login.gate = asyncio.Event()
    result = await _open_bankid(hass)
    placeholders = result["description_placeholders"]
    qr_url = placeholders["qr_url"]
    assert placeholders["qr_link_start"] == f'<a href="{qr_url}" target="_blank">'
    assert placeholders["qr_link_end"] == "</a>"

    # Shown again while the attempt runs, the dialog keeps the same link.
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["description_placeholders"]["qr_link_start"] == (
        f'<a href="{qr_url}" target="_blank">'
    )

    mock_bankid_login.gate.set()
    await hass.async_block_till_done()


async def test_qr_page_says_how_the_attempt_ended(
    hass, hass_client_no_auth, mock_info_mentor_api, mock_bankid_login
) -> None:
    """After a failed attempt the page shows the outcome, never a QR."""
    mock_bankid_login.outcome = "connection"
    result = await _open_bankid(hass)
    qr_path = urlsplit(result["description_placeholders"]["qr_url"]).path
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    frame = await (await client.get(qr_path + "/frame")).json()
    assert frame == {"matrix": None, "status": "failed", "launch_url": None}

    # Moving the dialog on closes the path at once.
    result = await _finish_bankid(hass, result["flow_id"])
    assert result["errors"] == {"base": "cannot_connect"}
    assert (await client.get(qr_path)).status == 410


async def test_qr_page_outcome_expires_without_the_dialog(
    hass, hass_client_no_auth, mock_info_mentor_api, mock_bankid_login
) -> None:
    """The outcome is shown for a bounded time even if nobody returns."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import (
        async_fire_time_changed,
    )

    from custom_components.infomentor.config_flow import FINAL_STATUS_SECONDS

    mock_bankid_login.outcome = "cancelled"
    result = await _open_bankid(hass)
    qr_path = urlsplit(result["description_placeholders"]["qr_url"]).path
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    assert (await client.get(qr_path)).status == 200

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=FINAL_STATUS_SECONDS + 1)
    )
    await hass.async_block_till_done()
    assert (await client.get(qr_path)).status == 410


async def test_waiting_dialog_moves_on_without_submit(
    hass, mock_info_mentor_api, mock_bankid_login
) -> None:
    """When BankID approves, Home Assistant tells the dialog to move on.

    The frontend continues a progress step on ``data_entry_flow_progressed``;
    a form would wait for Submit even after the login was done.
    """
    from pytest_homeassistant_custom_component.common import async_capture_events

    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    progressed = async_capture_events(hass, "data_entry_flow_progressed")
    gate = asyncio.Event()
    mock_bankid_login.gate = gate
    result = await _open_bankid(hass, option="bankid_phone")
    # The login task is still waiting for BankID; nothing tells the dialog
    # to move yet. (block_till_done would wait for the gated task itself.)
    await asyncio.sleep(0)
    assert not [e for e in progressed if e.data["flow_id"] == result["flow_id"]]

    gate.set()
    await hass.async_block_till_done()
    assert [e for e in progressed if e.data["flow_id"] == result["flow_id"]]

    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
