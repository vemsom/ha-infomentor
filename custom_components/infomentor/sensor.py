"""Sensor platform: per-child school status sensors and hub diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER
from .coordinator import InfoMentorCoordinator
from .entity import InfoMentorEntity

MAX_ATTRIBUTE_ITEMS = 10
MAX_TITLE_LENGTH = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the sensors for every configured child plus hub diagnostics."""
    coordinator: InfoMentorCoordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = []
    for child in coordinator.children:
        entities.extend(
            (
                InfoMentorUnreadSensor(coordinator, child),
                InfoMentorLatestMessageSensor(coordinator, child),
                InfoMentorNewsSensor(coordinator, child),
                InfoMentorNextEventSensor(coordinator, child),
                InfoMentorSessionSensor(coordinator, child),
            )
        )
    entities.append(InfoMentorLastUpdateSensor(coordinator, entry))
    async_add_entities(entities)


class InfoMentorUnreadSensor(InfoMentorEntity, SensorEntity):
    """Number of notifications with state New for the child."""

    _attr_name = "Olästa skolhändelser"
    _attr_icon = "mdi:school-outline"
    _attr_native_unit_of_measurement = "st"

    def __init__(self, coordinator: InfoMentorCoordinator, child) -> None:
        super().__init__(coordinator, child, "unread_notifications")

    @property
    def native_value(self) -> int | None:
        data = self.data
        return None if data is None else data.unread_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.data
        if data is None:
            return {}
        return {
            "notifications": [
                {
                    "title": item.title,
                    "sent_at": item.sent_at,
                    "type": item.notification_type,
                    "subject": item.subject,
                    "body": item.body,
                }
                for item in data.notifications[:MAX_ATTRIBUTE_ITEMS]
            ]
        }


class InfoMentorLatestMessageSensor(InfoMentorEntity, SensorEntity):
    """Newest school message: the text as the state, details as attributes.

    The state is capped at Home Assistant's 255-character limit; the full
    text is always available in the ``body`` attribute.
    """

    _attr_name = "Senaste meddelande"
    _attr_icon = "mdi:email-text-outline"

    def __init__(self, coordinator: InfoMentorCoordinator, child) -> None:
        super().__init__(coordinator, child, "latest_message")

    def _message(self):
        data = self.data
        if data is None:
            return None
        for item in data.notifications:
            if (item.notification_type or "") == "MessageCreated" and item.body:
                return item
        return None

    @property
    def native_value(self) -> str | None:
        item = self._message()
        if item is None:
            return None
        text = " ".join((item.body or "").split())
        if not text:
            return item.subject
        return text if len(text) <= MAX_TITLE_LENGTH else text[: MAX_TITLE_LENGTH - 1] + "\u2026"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._message()
        if item is None:
            return {}
        return {
            "subject": item.subject,
            "body": item.body,
            "sent_at": item.sent_at,
            "notification_id": item.id,
        }


class InfoMentorNewsSensor(InfoMentorEntity, SensorEntity):
    """Title of the newest school news item."""

    _attr_name = "Senaste nyhet"
    _attr_icon = "mdi:newspaper-variant-outline"

    def __init__(self, coordinator: InfoMentorCoordinator, child) -> None:
        super().__init__(coordinator, child, "latest_news")

    @property
    def native_value(self) -> str | None:
        data = self.data
        if data is None or (item := data.latest_news) is None:
            return None
        return (item.title or "Nyhet utan rubrik")[:MAX_TITLE_LENGTH]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.data
        if data is None or (item := data.latest_news) is None:
            return {}
        return {
            "published_at": item.published_at,
            "news_id": item.id,
            "title": item.title,
            "content": item.content,
        }


class InfoMentorNextEventSensor(InfoMentorEntity, SensorEntity):
    """Title and time of the child's next calendar event."""

    _attr_name = "Nästa händelse"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: InfoMentorCoordinator, child) -> None:
        super().__init__(coordinator, child, "next_event")

    @property
    def native_value(self) -> str | None:
        data = self.data
        if data is None or (event := data.next_event) is None:
            return None
        return event.title[:MAX_TITLE_LENGTH]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.data
        if data is None or (event := data.next_event) is None:
            return {}
        return {
            "title": event.title,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "all_day": event.all_day,
            "event_id": event.id,
        }


class InfoMentorSessionSensor(InfoMentorEntity, SensorEntity):
    """Session diagnostics: ok, or reauth needed when the token was rejected."""

    _attr_name = "Session"
    _attr_icon = "mdi:shield-key-outline"

    def __init__(self, coordinator: InfoMentorCoordinator, child) -> None:
        super().__init__(coordinator, child, "session")

    @property
    def available(self) -> bool:
        # Stay visible even when polling fails, so the reason is readable.
        return True

    @property
    def native_value(self) -> str:
        return "reauth_required" if self.coordinator.reauth_required else "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last_success = self.coordinator.last_successful_update
        exception = self.coordinator.last_exception
        return {
            "last_update": last_success.isoformat() if last_success else None,
            "last_update_success": self.coordinator.last_update_success,
            "include_childcare": self.coordinator.include_childcare,
            "notification": str(exception) if exception else None,
        }


class InfoMentorLastUpdateSensor(SensorEntity):
    """Last successful poll of the InfoMentor account (hub-level)."""

    _attr_has_entity_name = True
    _attr_name = "Senaste uppdatering"
    _attr_icon = "mdi:update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: InfoMentorCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_last_update"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=MANUFACTURER,
            manufacturer=MANUFACTURER,
            model="Hub",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> datetime | None:
        return self._coordinator.last_successful_update

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"reauth_required": self._coordinator.reauth_required}
