"""Button platform for Tuya BLE lock."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .entity import TuyaBLELockEntity
from .models import TuyaBLELockData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    data: TuyaBLELockData = entry.runtime_data
    entities = []
    for mac, coordinator in data.coordinators.items():
        entities.append(TuyaBLERefreshStatusButton(coordinator, entry))
        entities.append(TuyaBLECloudRefreshButton(coordinator, entry))
    if entities:
        async_add_entities(entities)


class TuyaBLERefreshStatusButton(TuyaBLELockEntity, ButtonEntity):
    """Pull the lock's current state over BLE (battery, motor, settings)."""

    _attr_name = "Refresh status"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self):
        return f"{self._mac}_refresh"

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class TuyaBLECloudRefreshButton(TuyaBLELockEntity, ButtonEntity):
    """Re-sync keys and DP snapshot from the Tuya cloud for this lock.

    Use when the lock is out of BLE range, after a re-pair in the Tuya app
    (which rotates local_key/sec_key/check_code), or to populate
    event-only sensors (doorbell, hijack, last unlock) from cloud state.
    """

    _attr_name = "Refresh via cloud"
    _attr_icon = "mdi:cloud-refresh"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self):
        return f"{self._mac}_cloud_refresh"

    async def async_press(self) -> None:
        from .tuya_cloud import async_refresh_one_device
        try:
            await async_refresh_one_device(
                self.hass, self._entry, self._mac,
            )
        except RuntimeError as exc:
            raise HomeAssistantError(str(exc)) from exc
        # Pick up fresh credentials + seeded cloud DPs
        await self.hass.config_entries.async_reload(self._entry.entry_id)
