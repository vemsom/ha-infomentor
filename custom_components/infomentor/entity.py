"""Shared entity base for InfoMentor platform entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Child, ChildData
from .const import DOMAIN, MANUFACTURER
from .coordinator import InfoMentorCoordinator


class InfoMentorEntity(CoordinatorEntity[InfoMentorCoordinator]):
    """One entity belonging to a child's InfoMentor device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: InfoMentorCoordinator, child: Child, key: str
    ) -> None:
        super().__init__(coordinator)
        self._child = child
        self._attr_unique_id = f"{child.id}_{key}"
        # The child's name is the device name, so entity ids become
        # calendar.<barn>_skola, sensor.<barn>_olasta_skolhandelser, ...
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, child.id)},
            name=child.name,
            manufacturer=MANUFACTURER,
            model="InfoMentor Hub",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def data(self) -> ChildData | None:
        """The last fetched data for this entity's child."""
        return self.coordinator.child_data(self._child.id)
