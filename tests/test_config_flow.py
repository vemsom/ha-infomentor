"""Config, options and reauth flow tests."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.infomentor.api import InfoMentorAuthError, child_scope_hash
from custom_components.infomentor.const import (
    CONF_CHILDREN,
    CONF_INCLUDE_CHILDCARE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)


async def test_user_flow_single_child(hass, mock_info_mentor_api) -> None:
    """A one-child account skips the child picker."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

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
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
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

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "BROKEN"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_entry_aborts(hass, mock_info_mentor_api) -> None:
    """The same child scope cannot be added twice."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "TOKEN"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "TOKEN2"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow_updates_token(hass, mock_info_mentor_api) -> None:
    """Reauth replaces the token on the existing entry."""
    mock_info_mentor_api.children = [{"id": "pupil-1", "name": "Ella"}]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="InfoMentor Ella",
        data={
            CONF_REFRESH_TOKEN: "OLD",
            CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}],
        },
        unique_id=child_scope_hash(["pupil-1"]),
        entry_id="entry-id",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "NEW"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "NEW"


async def test_reauth_rejects_other_account(hass, mock_info_mentor_api) -> None:
    """A token for a different child scope must not replace the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="InfoMentor Ella",
        data={
            CONF_REFRESH_TOKEN: "OLD",
            CONF_CHILDREN: [{"id": "pupil-1", "name": "Ella"}],
        },
        unique_id="some-other-scope",
        entry_id="entry-id-2",
    )
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
        result["flow_id"], {CONF_REFRESH_TOKEN: "NEW"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}
    assert entry.data[CONF_REFRESH_TOKEN] == "OLD"


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
