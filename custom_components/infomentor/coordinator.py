"""Data update coordinator for InfoMentor."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Child,
    ChildData,
    InfoMentorApi,
    InfoMentorAuthError,
    InfoMentorError,
)
from .const import (
    CALENDAR_DAYS_AHEAD,
    CHILDCARE_DAYS_AHEAD,
    CONF_CHILDREN,
    CONF_INCLUDE_CHILDCARE,
    DEFAULT_INCLUDE_CHILDCARE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    NEWS_LIMIT,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class InfoMentorData:
    """The coordinator payload: one entry per configured child."""

    children: dict[str, ChildData] = field(default_factory=dict)
    last_update: datetime | None = None


class InfoMentorCoordinator(DataUpdateCoordinator[InfoMentorData]):
    """Poll InfoMentor every 15 minutes for the configured children."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: InfoMentorApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
            config_entry=entry,
        )
        self.api = api
        self.children: list[Child] = [
            Child(id=item["id"], name=item["name"])
            for item in entry.data.get(CONF_CHILDREN, [])
        ]
        self.reauth_required = False
        self.last_successful_update: datetime | None = None

    @property
    def include_childcare(self) -> bool:
        """Whether the optional childcare schedule is fetched."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_CHILDCARE, DEFAULT_INCLUDE_CHILDCARE
            )
        )

    def child_data(self, child_id: str) -> ChildData | None:
        """Return the last fetched data for one child."""
        if self.data is None:
            return None
        return self.data.children.get(child_id)

    async def _async_update_data(self) -> InfoMentorData:
        if not self.children:
            raise UpdateFailed("No child is configured for this InfoMentor entry")

        today = dt_util.now().date()
        try:
            results = await self.api.async_fetch(
                [child.id for child in self.children],
                news_limit=NEWS_LIMIT,
                calendar_start=today,
                calendar_end=today + timedelta(days=CALENDAR_DAYS_AHEAD),
                include_childcare=self.include_childcare,
                childcare_end=today + timedelta(days=CHILDCARE_DAYS_AHEAD),
            )
        except InfoMentorAuthError as err:
            self.reauth_required = True
            raise ConfigEntryAuthFailed(str(err)) from err
        except InfoMentorError as err:
            raise UpdateFailed(str(err)) from err

        self.reauth_required = False
        self.last_successful_update = dt_util.utcnow()
        _LOGGER.debug(
            "InfoMentor update completed for %s child(ren); childcare=%s",
            len(results),
            self.include_childcare,
        )
        return InfoMentorData(
            children={item.child.id: item for item in results},
            last_update=self.last_successful_update,
        )
