"""Calendar platform: one school calendar per child."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import ChildcareDay, SchoolEvent
from .coordinator import InfoMentorCoordinator
from .entity import InfoMentorEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one calendar per configured child."""
    coordinator: InfoMentorCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        InfoMentorSchoolCalendar(coordinator, child) for child in coordinator.children
    )


class InfoMentorSchoolCalendar(InfoMentorEntity, CalendarEntity):
    """The child's InfoMentor calendar (school events and optional childcare)."""

    _attr_name = "Skola"

    def __init__(self, coordinator: InfoMentorCoordinator, child) -> None:
        super().__init__(coordinator, child, "school_calendar")

    @property
    def _events(self) -> list[CalendarEvent]:
        data = self.data
        if data is None:
            return []
        events = [self._as_calendar_event(event) for event in data.events]
        for day in data.childcare:
            childcare_event = self._as_childcare_event(day)
            if childcare_event is not None:
                events.append(childcare_event)
        return events

    @staticmethod
    def _as_calendar_event(event: SchoolEvent) -> CalendarEvent:
        if event.all_day:
            start: date | datetime = event.start.date()
            end: date | datetime = event.end.date() + timedelta(days=1)
        else:
            start, end = event.start, event.end
        return CalendarEvent(
            start=start,
            end=end,
            summary=event.title,
            description=event.description,
            uid=event.id,
        )

    @staticmethod
    def _as_childcare_event(day: ChildcareDay) -> CalendarEvent | None:
        if day.starts_at is None or day.ends_at is None:
            return None
        if day.is_closed or day.on_leave:
            return None
        summary = (
            f"Fritids {day.starts_at.strftime('%H:%M')}-{day.ends_at.strftime('%H:%M')}"
        )
        return CalendarEvent(
            start=day.starts_at,
            end=day.ends_at,
            summary=summary,
            description="Fritidshem (InfoMentor)",
            uid=f"childcare-{day.day.isoformat()}",
        )

    @property
    def event(self) -> CalendarEvent | None:
        """The current or next event, used for the calendar's state."""
        now = dt_util.now()
        upcoming = sorted(
            (event for event in self._events if self._end(event) >= now),
            key=lambda event: self._start(event),
        )
        return upcoming[0] if upcoming else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return events overlapping the requested window."""
        window_start = start_date
        window_end = end_date
        return [
            event
            for event in self._events
            if self._end(event) >= window_start and self._start(event) <= window_end
        ]

    @staticmethod
    def _start(event: CalendarEvent) -> datetime:
        return (
            dt_util.start_of_local_day(event.start)
            if isinstance(event.start, date) and not isinstance(event.start, datetime)
            else event.start
        )

    @staticmethod
    def _end(event: CalendarEvent) -> datetime:
        return (
            dt_util.start_of_local_day(event.end)
            if isinstance(event.end, date) and not isinstance(event.end, datetime)
            else event.end
        )
