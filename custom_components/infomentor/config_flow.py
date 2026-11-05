"""Config and options flow for InfoMentor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from html import escape
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later

from .api import (
    Child,
    InfoMentorApi,
    InfoMentorAuthError,
    InfoMentorConnectionError,
    InfoMentorError,
    child_scope_hash,
)
from .bankid import BankIdError, BankIdLogin, _trace
from .const import (
    CONF_CHILDREN,
    CONF_INCLUDE_CHILDCARE,
    CONF_LINKED_CHILDREN,
    CONF_REFRESH_TOKEN,
    DEFAULT_INCLUDE_CHILDCARE,
    DOMAIN,
    ENTRY_TITLE,
)
from .qr import QrSession, async_get_store

_LOGGER = logging.getLogger(__name__)

TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_REFRESH_TOKEN): str})
EMPTY_SCHEMA = vol.Schema({})
# How long the options dialog waits for the current list of linked pupils.
LINKED_CHILDREN_TIMEOUT = 30


def _child_dicts(children: list[Child]) -> list[dict[str, str]]:
    return [{"id": child.id, "name": child.name} for child in children]


def _stored_children(entry: ConfigEntry, key: str) -> list[Child]:
    return [Child(id=item["id"], name=item["name"]) for item in entry.data.get(key, [])]


def _children_in_other_entries(hass: HomeAssistant, entry_id: str | None) -> set[str]:
    """Pupils that already have entities through another InfoMentor entry.

    Entity and device ids are per pupil, so one pupil can only be selected in
    one entry. Two guardians who share a child each get the children the other
    one has not added.
    """
    taken: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id != entry_id:
            taken.update(item["id"] for item in entry.data.get(CONF_CHILDREN, []))
    return taken


async def _async_validate_token(
    hass: HomeAssistant, refresh_token: str
) -> tuple[InfoMentorApi, list[Child]]:
    """Prove a token with a session of its own, then let the session go.

    The login cookies must not land in a session that another integration or
    the running entry uses. The connector is Home Assistant's, so the session
    is detached rather than closed.
    """
    session = async_create_clientsession(hass, auto_cleanup=False)
    try:
        api = InfoMentorApi(session, refresh_token)
        children = await api.async_validate()
    finally:
        session.detach()
    return api, children


def _new_bankid_cookie_jar(*, unsafe: bool = False) -> aiohttp.CookieJar:
    """A cookie jar that sends every value back exactly as it was set.

    aiohttp's default jar re-quotes a cookie value that holds characters such
    as ``/`` or ``=``: it sends ``SMSESSION="AbC/dE=="`` where the server set
    ``SMSESSION=AbC/dE==``. Stockholm's SiteMinder session cookie is base64, so
    it always contains them. With the quotes, login001 cannot read the session
    that SiteMinderAuthADC has just issued for the approved BankID order,
    challenges again and lands on the BankID chooser page: the approval is
    lost one hop after it succeeded. Browsers and httpx (the reference client)
    send the value unchanged, so this jar does too.
    """
    return aiohttp.CookieJar(unsafe=unsafe, quote_cookie=False)


def _new_bankid_session() -> aiohttp.ClientSession:
    """A private session with its own connector and cookie jar.

    The BankID login must not share a connector with Home Assistant or with the
    runtime client: closing a session we own is the only way to end an attempt,
    and a shared connector would take every other integration down with it.
    The jar must not quote cookie values; see ``_new_bankid_cookie_jar``.
    """
    return aiohttp.ClientSession(cookie_jar=_new_bankid_cookie_jar())


STEP_BANKID = "bankid"
STEP_BANKID_PHONE = "bankid_phone"
STEP_TOKEN = "token"
MENU_OPTIONS = [STEP_BANKID, STEP_BANKID_PHONE, STEP_TOKEN]
# The waiting dialog's text per method: the QR page, or the app link.
PROGRESS_ACTIONS = {
    STEP_BANKID: "bankid_scan",
    STEP_BANKID_PHONE: "bankid_phone_scan",
}

# BankID login outcomes that the form can explain, mapped to error keys.
BANKID_ERRORS = {
    "timeout": "bankid_timeout",
    "cancelled": "bankid_cancelled",
    "invalid_auth": "invalid_auth",
    "connection": "cannot_connect",
}

# How an attempt can end, as BankIdLogin reports it.
FINAL_STATUSES = frozenset({"complete", "failed", "cancelled", "timeout"})
# How long the QR page may keep saying how the attempt ended, at most. The
# path closes earlier when the dialog moves on or is abandoned.
FINAL_STATUS_SECONDS = 120


def _qr_link_placeholders(url: str) -> dict[str, str]:
    """Placeholders that make the QR page link open in a tab of its own.

    A markdown link to Home Assistant's own address is caught by the
    frontend's router, which shows the dashboard instead of the page. A link
    with ``target`` is left to the browser. The tag is passed as placeholder
    values because the frontend's message formatter rejects an HTML tag
    written in the translation itself (``INVALID_TAG``).
    """
    if not url:
        return {"qr_url": "", "qr_link_start": "", "qr_link_end": ""}
    return {
        "qr_url": url,
        "qr_link_start": f'<a href="{escape(url, quote=True)}" target="_blank">',
        "qr_link_end": "</a>",
    }


class InfoMentorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with BankID (default) or a pasted refresh token."""

    VERSION = 1

    def __init__(self) -> None:
        self._refresh_token: str = ""
        self._children: list[Child] = []
        self._linked: list[Child] = []
        self._reauth_entry: ConfigEntry | None = None
        # One BankID attempt at a time; config and reauth share the machinery.
        self._login: BankIdLogin | None = None
        self._login_task: asyncio.Task[None] | None = None
        self._qr: QrSession | None = None
        self._bankid_session: aiohttp.ClientSession | None = None
        self._bankid_method = STEP_BANKID
        self._bankid_error: dict[str, str] = {}
        self._qr_expire: CALLBACK_TYPE | None = None

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
        """Same device: the SSO's own autostart link opens the BankID app."""
        return await self._async_bankid_step(STEP_BANKID_PHONE, user_input)

    async def _async_bankid_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Run a login in a waiting dialog that moves on by itself.

        Home Assistant never refreshes a form when a background task ends, so
        a form left people pressing Submit after approving in BankID. A
        progress step is continued by Home Assistant the moment the login task
        is done, and its text carries the same links. A form is shown only
        after a failed attempt, to say why; its Submit starts the next one, so
        no BankID order runs while nobody is looking.
        """
        self._bankid_method = step_id
        if self._login is not None:
            return self._async_show_bankid_progress()
        if self._bankid_error and user_input is None:
            errors, self._bankid_error = self._bankid_error, {}
            return self.async_show_form(
                step_id=step_id, data_schema=EMPTY_SCHEMA, errors=errors
            )
        self._bankid_error = {}
        method = "qr" if step_id == STEP_BANKID else "same_device"
        if not await self._async_start_bankid(method):
            # Without a BankID order there is no QR or app link to show.
            return self.async_abort(reason="bankid_unavailable")
        return self._async_show_bankid_progress()

    @callback
    def _async_show_bankid_progress(self) -> ConfigFlowResult:
        """The waiting dialog for the running attempt, with its links."""
        return self.async_show_progress(
            step_id="bankid_wait",
            progress_action=PROGRESS_ACTIONS[self._bankid_method],
            progress_task=self._login_task,
            description_placeholders={
                **self._qr_placeholders(),
                "launch_url": (self._login.launch_url or "") if self._login else "",
            },
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

    def _qr_placeholders(self) -> dict[str, str]:
        """The QR page link for the dialog text, or empty once it is closed."""
        return _qr_link_placeholders(self._qr.url if self._qr else "")

    @callback
    def _async_bankid_update(
        self, qr_data: str | None, status: str, launch_url: str | None
    ) -> None:
        """Publish the QR/status; after the attempt, only how it ended."""
        if self._qr is None:
            return
        if status in FINAL_STATUSES:
            # The attempt is over: no QR or app link is served again. The page
            # may still say how it ended, so the person who scanned is not
            # left with "the page is closed". The path stops working when the
            # dialog moves on or is abandoned, or after FINAL_STATUS_SECONDS.
            self._qr.update(None, status, None)
            if self._qr_expire is None:
                self._qr_expire = async_call_later(
                    self.hass,
                    FINAL_STATUS_SECONDS,
                    HassJob(self._async_expire_qr, cancel_on_shutdown=True),
                )
            return
        self._qr.update(qr_data, status, launch_url)

    @callback
    def _async_expire_qr(self, _now: datetime) -> None:
        """Stop serving the outcome once the grace period is over."""
        self._qr_expire = None
        if self._qr is not None:
            self._qr.close()
            self._qr = None

    async def async_step_bankid_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep the progress dialog alive until the attempt is over."""
        if self._login_task is None:
            return self.async_show_progress_done(next_step_id="bankid_result")
        if not self._login_task.done():
            # The text links the QR page and the app, so it needs the same
            # placeholders every time it is shown.
            return self._async_show_bankid_progress()
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

        try:
            # One read proves the grant; nothing is stored without it.
            api, children = await _async_validate_token(self.hass, refresh_token)
        except InfoMentorAuthError:
            _trace("validate rejected the new token")
            self._bankid_error = {"base": "invalid_auth"}
            return await self._async_bankid_step(self._bankid_method, None)
        except InfoMentorConnectionError as err:
            _trace(f"validate connection error: {err}")
            self._bankid_error = {"base": "cannot_connect"}
            return await self._async_bankid_step(self._bankid_method, None)
        except InfoMentorError as err:
            _trace(f"validate error {type(err).__name__}")
            _LOGGER.debug("InfoMentor BankID validation failed unexpectedly")
            self._bankid_error = {"base": "unknown"}
            return await self._async_bankid_step(self._bankid_method, None)
        _trace(f"validate ok children={len(children)}")

        result = await self._async_finish_login(api, children)
        if result is None:
            self._bankid_error = {"base": "wrong_account"}
            return await self._async_bankid_step(self._bankid_method, None)
        return result

    async def _async_close_bankid(self) -> None:
        """Stop serving the QR path and release the login transport."""
        if self._qr_expire is not None:
            self._qr_expire()
            self._qr_expire = None
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
            try:
                api, children = await _async_validate_token(self.hass, token)
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
        # Only the same set of pupils counts as the same account. Another
        # guardian, or a second account, gets an entry of its own.
        self._abort_if_unique_id_configured()
        self._linked = children
        taken = _children_in_other_entries(self.hass, None)
        self._children = [child for child in children if child.id not in taken]
        if not self._children:
            return self.async_abort(reason="children_already_configured")
        if len(self._children) == 1:
            return self._async_create_entry(self._children)
        return await self.async_step_children()

    @callback
    def _async_finish_reauth(
        self, children: list[Child], refresh_token: str
    ) -> ConfigFlowResult | None:
        """Replace the token on the existing entry, or refuse another account.

        The login belongs to this entry when it reaches at least one of the
        entry's selected pupils. A sibling linked or unlinked since the entry
        was made does not count as another account.
        """
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="reauth_failed")
        names = {child.id: child.name for child in children}
        selected = entry.data.get(CONF_CHILDREN, [])
        still_selected = [
            {"id": item["id"], "name": names[item["id"]]}
            for item in selected
            if item["id"] in names
        ]
        if not still_selected:
            # A login for a different account must not replace this entry.
            return None
        scope = child_scope_hash([child.id for child in children])
        scope_taken = any(
            other.unique_id == scope
            for other in self.hass.config_entries.async_entries(DOMAIN)
            if other.entry_id != entry.entry_id
        )
        return self.async_update_reload_and_abort(
            entry,
            unique_id=entry.unique_id if scope_taken else scope,
            data_updates={
                CONF_REFRESH_TOKEN: refresh_token,
                CONF_CHILDREN: still_selected,
                CONF_LINKED_CHILDREN: _child_dicts(children),
            },
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
        # The title stays neutral: Home Assistant writes it to the log.
        return self.async_create_entry(
            title=ENTRY_TITLE,
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_CHILDREN: _child_dicts(children),
                CONF_LINKED_CHILDREN: _child_dicts(self._linked),
            },
            options={CONF_INCLUDE_CHILDCARE: DEFAULT_INCLUDE_CHILDCARE},
        )

    # -- cleanup ------------------------------------------------------------ #

    @callback
    def async_remove(self) -> None:
        """Make sure an abandoned dialog cannot leave a QR path behind."""
        if (
            self._qr is None
            and self._login is None
            and self._bankid_session is None
            and self._qr_expire is None
        ):
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
        if user_input is None:
            await self._async_refresh_linked_children()
        selected = _stored_children(entry, CONF_CHILDREN)
        # Entries made before 0.1.10 only stored the selected children.
        linked = _stored_children(entry, CONF_LINKED_CHILDREN) or selected
        taken = _children_in_other_entries(self.hass, entry.entry_id)
        available = [child for child in linked if child.id not in taken]
        available_ids = {child.id for child in available}
        available += [child for child in selected if child.id not in available_ids]
        options = {child.id: child.name for child in available}

        if user_input is not None:
            chosen = [
                child
                for child in available
                if child.id in set(user_input[CONF_CHILDREN])
            ]
            if not chosen:
                errors["base"] = "no_children"
            else:
                new_options = {
                    **entry.options,
                    CONF_INCLUDE_CHILDCARE: user_input[CONF_INCLUDE_CHILDCARE],
                }
                changed = new_options != dict(entry.options) or [
                    child.id for child in chosen
                ] != [child.id for child in selected]
                # Both are saved here, before the reload below can start.
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_CHILDREN: _child_dicts(chosen)},
                    options=new_options,
                )
                if changed:
                    # One reload for the whole change. The entry has no update
                    # listener, so a token rotation never reloads it.
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CHILDREN,
                        default=[child.id for child in selected],
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

    async def _async_refresh_linked_children(self) -> None:
        """Ask InfoMentor which pupils are linked now, so new ones can be picked.

        Only when the entry is running. Any failure keeps the stored list.
        """
        entry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return
        try:
            async with asyncio.timeout(LINKED_CHILDREN_TIMEOUT):
                children = await entry.runtime_data.api.async_get_children(
                    with_names=True
                )
        except (InfoMentorError, TimeoutError):
            _LOGGER.debug("Could not list the linked InfoMentor pupils")
            return
        linked = _child_dicts(children)
        if linked != entry.data.get(CONF_LINKED_CHILDREN):
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_LINKED_CHILDREN: linked}
            )
