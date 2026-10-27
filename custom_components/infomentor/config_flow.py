"""Config and options flow for InfoMentor."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    Child,
    InfoMentorApi,
    InfoMentorAuthError,
    InfoMentorConnectionError,
    InfoMentorError,
    child_scope_hash,
)
from .const import (
    CONF_CHILDREN,
    CONF_INCLUDE_CHILDCARE,
    CONF_REFRESH_TOKEN,
    DEFAULT_INCLUDE_CHILDCARE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_REFRESH_TOKEN): str})


class InfoMentorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for a refresh token, then for the children to expose."""

    VERSION = 1

    def __init__(self) -> None:
        self._refresh_token: str = ""
        self._children: list[Child] = []
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate a pasted refresh token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_REFRESH_TOKEN].strip()
            api = InfoMentorApi(async_get_clientsession(self.hass), token)
            try:
                children = await api.async_validate()
            except InfoMentorAuthError:
                errors["base"] = "invalid_auth"
            except InfoMentorConnectionError:
                errors["base"] = "cannot_connect"
            except InfoMentorError:
                _LOGGER.debug(
                    "InfoMentor validation failed with an unexpected response"
                )
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    child_scope_hash([child.id for child in children])
                )
                self._async_abort_entries_match()
                # Keep the token exactly as returned: it may have rotated.
                self._refresh_token = api.refresh_token
                self._children = children
                if len(children) == 1:
                    return self._async_create_entry(children)
                return await self.async_step_children()

        return self.async_show_form(
            step_id="user", data_schema=TOKEN_SCHEMA, errors=errors
        )

    async def async_step_children(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which linked children get entities."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected_ids = user_input[CONF_CHILDREN]
            selected = [
                child for child in self._children if child.id in set(selected_ids)
            ]
            if not selected:
                errors["base"] = "no_children"
            else:
                return self._async_create_entry(selected)

        options = {child.id: child.name for child in self._children}
        return self.async_show_form(
            step_id="children",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CHILDREN,
                        default=list(options),
                    ): cv.multi_select(options)
                }
            ),
            errors=errors,
        )

    @callback
    def _async_create_entry(self, children: list[Child]) -> ConfigFlowResult:
        return self.async_create_entry(
            title="InfoMentor " + ", ".join(child.name for child in children),
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_CHILDREN: [
                    {"id": child.id, "name": child.name} for child in children
                ],
            },
            options={CONF_INCLUDE_CHILDCARE: DEFAULT_INCLUDE_CHILDCARE},
        )

    # -- reauth ------------------------------------------------------------- #

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication when the refresh token was rejected."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh refresh token for the existing entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api = InfoMentorApi(
                async_get_clientsession(self.hass),
                user_input[CONF_REFRESH_TOKEN].strip(),
            )
            try:
                children = await api.async_validate()
            except InfoMentorAuthError:
                errors["base"] = "invalid_auth"
            except InfoMentorConnectionError:
                errors["base"] = "cannot_connect"
            except InfoMentorError:
                _LOGGER.debug("InfoMentor reauth validation failed unexpectedly")
                errors["base"] = "unknown"
            else:
                entry = self._reauth_entry
                if entry is None:
                    return self.async_abort(reason="reauth_failed")
                scope = child_scope_hash([child.id for child in children])
                if entry.unique_id is not None and entry.unique_id != scope:
                    # A token for a different account must not replace this entry.
                    errors["base"] = "wrong_account"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, CONF_REFRESH_TOKEN: api.refresh_token},
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=TOKEN_SCHEMA, errors=errors
        )

    # -- options ------------------------------------------------------------ #

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> InfoMentorOptionsFlow:
        return InfoMentorOptionsFlow()


class InfoMentorOptionsFlow(OptionsFlow):
    """Toggle the optional childcare schedule and the exposed children."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        configured = [
            Child(id=item["id"], name=item["name"])
            for item in entry.data.get(CONF_CHILDREN, [])
        ]
        options = {child.id: child.name for child in configured}

        if user_input is not None:
            selected = [
                child
                for child in configured
                if child.id in set(user_input[CONF_CHILDREN])
            ]
            if not selected:
                errors["base"] = "no_children"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_CHILDREN: [
                            {"id": child.id, "name": child.name} for child in selected
                        ],
                    },
                )
                return self.async_create_entry(
                    title="",
                    data={CONF_INCLUDE_CHILDCARE: user_input[CONF_INCLUDE_CHILDCARE]},
                )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CHILDREN,
                        default=[child.id for child in configured],
                    ): cv.multi_select(options),
                    vol.Required(
                        CONF_INCLUDE_CHILDCARE,
                        default=entry.options.get(
                            CONF_INCLUDE_CHILDCARE, DEFAULT_INCLUDE_CHILDCARE
                        ),
                    ): bool,
                }
            ),
            errors=errors,
        )
