"""Config and options flow for InfoMentor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
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
from .bankid import BankIdError, BankIdLogin
from .const import (
    CONF_CHILDREN,
    CONF_INCLUDE_CHILDCARE,
    CONF_REFRESH_TOKEN,
    DEFAULT_INCLUDE_CHILDCARE,
    DOMAIN,
)
from .qr import QrSession, async_get_store

_LOGGER = logging.getLogger(__name__)

TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_REFRESH_TOKEN): str})
EMPTY_SCHEMA = vol.Schema({})


def _new_bankid_session() -> aiohttp.ClientSession:
    """A private session with its own connector and cookie jar.

    The BankID login must not share a connector with Home Assistant or with the
    runtime client: closing a session we own is the only way to end an attempt,
    and a shared connector would take every other integration down with it.
    """
    return aiohttp.ClientSession()


STEP_BANKID = "bankid"
STEP_BANKID_PHONE = "bankid_phone"
STEP_TOKEN = "token"
MENU_OPTIONS = [STEP_BANKID, STEP_BANKID_PHONE, STEP_TOKEN]

# BankID login outcomes that the form can explain, mapped to error keys.
BANKID_ERRORS = {
    "timeout": "bankid_timeout",
    "cancelled": "bankid_cancelled",
    "invalid_auth": "invalid_auth",
    "connection": "cannot_connect",
}


class InfoMentorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with BankID (default) or a pasted refresh token."""

    VERSION = 1

    def __init__(self) -> None:
        self._refresh_token: str = ""
        self._children: list[Child] = []
        self._reauth_entry: ConfigEntry | None = None
        # One BankID attempt at a time; config and reauth share the machinery.
        self._login: BankIdLogin | None = None
        self._login_task: asyncio.Task[None] | None = None
        self._qr: QrSession | None = None
        self._bankid_session: aiohttp.ClientSession | None = None
        self._bankid_method = STEP_BANKID
        self._bankid_error: dict[str, str] = {}

    # -- start -------------------------------------------------------------- #

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the phone-friendly BankID login first."""
        return self.async_show_menu(step_id="user", menu_options=MENU_OPTIONS)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Reauthenticate when InfoMentor rejected the refresh token."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return self.async_show_menu(step_id="reauth", menu_options=MENU_OPTIONS)

    # -- BankID ------------------------------------------------------------- #

    async def async_step_bankid(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the rotating BankID QR for scanning on another device."""
        return await self._async_bankid_step(STEP_BANKID, user_input)

    async def async_step_bankid_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the same-device BankID link when only one phone is at hand."""
        return await self._async_bankid_step(STEP_BANKID_PHONE, user_input)

    async def _async_bankid_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Start a login, or hand the running attempt to the progress dialog."""
        self._bankid_method = step_id
        if self._login is not None and user_input is not None:
            return self.async_show_progress(
                step_id="bankid_wait",
                progress_action="bankid_scan",
                progress_task=self._login_task,
            )

        errors: dict[str, str] = {}
        if self._login is None:
            errors = self._bankid_error
            self._bankid_error = {}
            method = "qr" if step_id == STEP_BANKID else "same_device"
            if not await self._async_start_bankid(method):
                # Without a BankID order there is no QR to show.
                return self.async_abort(reason="bankid_unavailable")
        return self.async_show_form(
            step_id=step_id,
            data_schema=EMPTY_SCHEMA,
            errors=errors,
            description_placeholders={"qr_url": self._qr.url if self._qr else ""},
        )

    async def _async_start_bankid(self, method: str) -> bool:
        """Create the QR path and start a BankID order with its polling task."""
        if self._qr is None:
            self._qr = async_get_store(self.hass).open()
        if self._bankid_session is None:
            # A private session with its own connector and cookie jar: the SSO
            # cookies for this attempt never reach the runtime client, and
            # closing it cannot touch anything Home Assistant owns.
            self._bankid_session = _new_bankid_session()
        login = BankIdLogin(
            self._bankid_session, method=method, on_update=self._async_bankid_update
        )
        try:
            await login.async_start()
        except InfoMentorConnectionError:
            _LOGGER.debug("InfoMentor BankID could not be started (network error)")
            await self._async_close_bankid()
            return False
        except BankIdError:
            _LOGGER.debug(
                "InfoMentor BankID could not be started (unexpected response)"
            )
            await self._async_close_bankid()
            return False
        self._login = login
        # Poll immediately: the QR page can only rotate the code while the
        # order is being polled, and a stale code cannot be scanned.
        self._login_task = self.hass.async_create_task(login.async_wait())
        return True

    @callback
    def _async_bankid_update(
        self, qr_data: str | None, status: str, launch_url: str | None
    ) -> None:
        """Publish the QR/status, and retire the path when the attempt ends."""
        if self._qr is None:
            return
        self._qr.update(qr_data, status, launch_url)
        if status in {"complete", "failed", "cancelled", "timeout"}:
            # The attempt is over: the one-time path must stop working.
            self._qr.close()
            self._qr = None

    async def async_step_bankid_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep the progress dialog alive until the attempt is over."""
        if self._login_task is None:
            return self.async_show_progress_done(next_step_id="bankid_result")
        if not self._login_task.done():
            return self.async_show_progress(
                step_id="bankid_wait",
                progress_action="bankid_scan",
                progress_task=self._login_task,
            )
        return self.async_show_progress_done(next_step_id="bankid_result")

    async def async_step_bankid_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish a BankID attempt: keep the token or explain what failed."""
        login = self._login
        if login is None:
            return await self._async_bankid_step(self._bankid_method, None)
        refresh_token = login.refresh_token
        error = login.error or ""
        await self._async_close_bankid()
        if refresh_token is None:
            self._bankid_error = {"base": BANKID_ERRORS.get(error, "unknown")}
            return await self._async_bankid_step(self._bankid_method, None)

        api = InfoMentorApi(async_get_clientsession(self.hass), refresh_token)
        try:
            # One read proves the grant; nothing is stored without it.
            children = await api.async_validate()
        except InfoMentorAuthError:
            self._bankid_error = {"base": "invalid_auth"}
            return await self._async_bankid_step(self._bankid_method, None)
        except InfoMentorConnectionError:
            self._bankid_error = {"base": "cannot_connect"}
            return await self._async_bankid_step(self._bankid_method, None)
        except InfoMentorError:
            _LOGGER.debug("InfoMentor BankID validation failed unexpectedly")
            self._bankid_error = {"base": "unknown"}
            return await self._async_bankid_step(self._bankid_method, None)

        result = await self._async_finish_login(api, children)
        if result is None:
            self._bankid_error = {"base": "wrong_account"}
            return await self._async_bankid_step(self._bankid_method, None)
        return result

    async def _async_close_bankid(self) -> None:
        """Stop serving the QR path and release the login transport."""
        if self._qr is not None:
            self._qr.close()
            self._qr = None
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
        self._login = None
        self._login_task = None
        if self._bankid_session is not None:
            session, self._bankid_session = self._bankid_session, None
            # The session and its connector are ours alone (see
            # _new_bankid_session); closing it cannot affect Home Assistant or
            # any other integration.
            await session.close()

    # -- refresh token (advanced) ------------------------------------------- #

    async def async_step_token(
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
                result = await self._async_finish_login(api, children)
                if result is not None:
                    return result
                errors["base"] = "wrong_account"

        return self.async_show_form(
            step_id=STEP_TOKEN, data_schema=TOKEN_SCHEMA, errors=errors
        )

    async def _async_finish_login(
        self, api: InfoMentorApi, children: list[Child]
    ) -> ConfigFlowResult | None:
        """Continue a validated login; ``None`` means the account differs."""
        # Keep the token exactly as returned: it may have rotated.
        self._refresh_token = api.refresh_token
        if self._reauth_entry is not None:
            return self._async_finish_reauth(children, self._refresh_token)
        await self.async_set_unique_id(
            child_scope_hash([child.id for child in children])
        )
        self._async_abort_entries_match()
        self._children = children
        if len(children) == 1:
            return self._async_create_entry(children)
        return await self.async_step_children()

    @callback
    def _async_finish_reauth(
        self, children: list[Child], refresh_token: str
    ) -> ConfigFlowResult | None:
        """Replace the token on the existing entry, or refuse another account."""
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="reauth_failed")
        scope = child_scope_hash([child.id for child in children])
        if entry.unique_id is not None and entry.unique_id != scope:
            # A login for a different account must not replace this entry.
            return None
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: refresh_token}
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(entry.entry_id)
        )
        return self.async_abort(reason="reauth_successful")

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

    # -- cleanup ------------------------------------------------------------ #

    @callback
    def async_remove(self) -> None:
        """Make sure an abandoned dialog cannot leave a QR path behind."""
        if self._qr is None and self._login is None and self._bankid_session is None:
            return
        self.hass.async_create_task(self._async_close_bankid())

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
