"""Set up the InfoMentor integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import InfoMentorApi
from .const import (
    CONF_REFRESH_TOKEN,
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
    session = async_get_clientsession(hass)

    def _store_rotated_token(refresh_token: str) -> None:
        """Persist a rotated refresh token before it can be lost."""
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
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: InfoMentorConfigEntry) -> bool:
    """Unload an InfoMentor config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.api.async_close()
    return unloaded


async def _async_reload_entry(
    hass: HomeAssistant, entry: InfoMentorConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entry versions (only version 1 exists so far)."""
    if entry.version > 1:
        _LOGGER.error(
            "Cannot migrate InfoMentor entry from version %s; reinstall required",
            entry.version,
        )
        return False
    return True
