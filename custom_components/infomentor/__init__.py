"""Set up the InfoMentor integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import InfoMentorApi
from .const import (
    CONF_CHILDREN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ENTRY_TITLE,
    PLATFORMS,
)
from .coordinator import InfoMentorCoordinator

_LOGGER = logging.getLogger(__name__)


type InfoMentorConfigEntry = ConfigEntry[InfoMentorRuntimeData]


@dataclass
class InfoMentorRuntimeData:
    """Objects stored on the config entry for the platforms to use."""

    api: InfoMentorApi
    coordinator: InfoMentorCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: InfoMentorConfigEntry) -> bool:
    """Set up InfoMentor from a config entry."""
    _async_neutral_title(hass, entry)

    # A session of its own: the Hub session and the selected pupil live in its
    # cookies, so they must not be shared with other integrations, a login
    # dialog or an earlier instance of this entry. Created during setup, it is
    # detached by Home Assistant when the entry is unloaded.
    session = async_create_clientsession(hass)

    def _store_rotated_token(refresh_token: str) -> None:
        """Persist a rotated refresh token before it can be lost.

        Updating the entry does not reload it: the entry has no update
        listener, so a rotation is saved without restarting the polling.
        """
        if refresh_token == entry.data.get(CONF_REFRESH_TOKEN):
            return
        # The old token is already invalid upstream; keep the new one on disk.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_REFRESH_TOKEN: refresh_token},
        )
        _LOGGER.debug("Stored a rotated InfoMentor refresh token")

    api = InfoMentorApi(
        session,
        entry.data[CONF_REFRESH_TOKEN],
        on_token_refresh=_store_rotated_token,
    )
    coordinator = InfoMentorCoordinator(hass, entry, api)

    # Raises ConfigEntryAuthFailed (reauth) or ConfigEntryNotReady on failure.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = InfoMentorRuntimeData(api=api, coordinator=coordinator)
    _async_remove_deselected_devices(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: InfoMentorConfigEntry) -> bool:
    """Unload an InfoMentor config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.api.async_close()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device unless it belongs to a selected child."""
    return not _device_ids(device) & _kept_ids(entry)


def _async_neutral_title(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the children's names from a title made by an older version.

    Home Assistant writes the entry title to its log when setup fails or is
    retried, so a title with the children's names put them in the log. A title
    the user has set is left alone.
    """
    names = [item.get("name", "") for item in entry.data.get(CONF_CHILDREN, [])]
    if names and entry.title == "InfoMentor " + ", ".join(names):
        hass.config_entries.async_update_entry(entry, title=ENTRY_TITLE)


def _kept_ids(entry: ConfigEntry) -> set[str]:
    """Device identifiers in use: the selected children and the account."""
    return {item["id"] for item in entry.data.get(CONF_CHILDREN, [])} | {entry.entry_id}


def _device_ids(device: dr.DeviceEntry) -> set[str]:
    return {value for domain, value in device.identifiers if domain == DOMAIN}


def _async_remove_deselected_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the devices of children that are no longer selected.

    Their entities go with them. Selecting the child again creates them anew.
    """
    registry = dr.async_get(hass)
    kept = _kept_ids(entry)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ids = _device_ids(device)
        if ids and not ids & kept:
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entry versions (only version 1 exists so far)."""
    if entry.version > 1:
        _LOGGER.error(
            "Cannot migrate InfoMentor entry from version %s; reinstall required",
            entry.version,
        )
        return False
    return True
