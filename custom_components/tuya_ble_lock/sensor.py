"""Sensor platform for Tuya BLE lock."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import TuyaBLELockEntity
from .models import TuyaBLELockData

_DIAG_KEYS = [
    ("uuid", "UUID", "uuid"),
    ("login_key", "Login key", "login_key"),
    ("virtual_id", "Virtual ID", "virtual_id"),
    ("auth_key", "Auth key", "auth_key"),
    # V5 / btScyChannel credentials — present only after reauth or on
    # freshly-added devices. Diagnostic so users can confirm reauth ran.
    ("local_key", "Local key", "local_key"),
    ("sec_key", "Sec key", "sec_key"),
    ("check_code", "Check code", "check_code"),
]


async def async_setup_entry(hass, entry, async_add_entities):
    data: TuyaBLELockData = entry.runtime_data
    entities = []
    for mac, coordinator in data.coordinators.items():
        profile = coordinator.profile or {}
        entities_cfg = profile.get("entities", {})
        if "battery_sensor" in entities_cfg:
            entities.append(TuyaBLEBatterySensor(coordinator, entry))
        state_map = profile.get("state_map", {})
        has_alarm = any(m.get("key") == "alarm_lock" for m in state_map.values())
        if has_alarm:
            entities.append(TuyaBLEAlarmSensor(coordinator, entry))
        has_door = any(m.get("key") == "closed_opened" for m in state_map.values())
        if has_door:
            entities.append(TuyaBLEDoorSensor(coordinator, entry))
        # Last-unlock sensor: show method only on locks that expose at least
        # one unlock-method DP in their state_map.
        if any(
            (m.get("key") or "").startswith("unlock_") for m in state_map.values()
        ):
            entities.append(TuyaBLELastUnlockSensor(coordinator, entry))
        for data_key, name, uid_suffix in _DIAG_KEYS:
            entities.append(
                TuyaBLEDiagnosticSensor(
                    coordinator,
                    entry,
                    data_key,
                    name,
                    uid_suffix,
                )
            )
    if entities:
        async_add_entities(entities)


BATTERY_STATE_TO_PERCENT = {
    "high": 100,
    "medium": 50,
    "low": 25,
    "exhausted": 5,
}


class TuyaBLEBatterySensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return f"{self._mac}_battery"

    @property
    def native_value(self) -> int | None:
        pct = self.coordinator.state.get("battery_percent")
        if pct is not None:
            return pct
        state = self.coordinator.state.get("battery_state")
        if state:
            return BATTERY_STATE_TO_PERCENT.get(state)
        alarm = self.coordinator.state.get("alarm_lock")
        if alarm == "low_battery" or alarm == 10:
            return 10
        return None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("battery_percent") is None:
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unknown", "unavailable"):
                try:
                    self.coordinator.state["battery_percent"] = int(float(last.state))
                except (ValueError, TypeError):
                    pass


_ALARM_LOCK_MAP = [
    "wrong_finger", "wrong_password", "wrong_card", "wrong_face",
    "tongue_bad", "too_hot", "unclosed_time", "tongue_not_out",
    "pry", "key_in", "low_battery", "power_off", "shock", "defense",
]


class TuyaBLEAlarmSensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    _attr_name = "Lock alarm"
    _attr_icon = "mdi:alert-circle"

    @property
    def unique_id(self):
        return f"{self._mac}_alarm"

    @property
    def native_value(self) -> str | None:
        val = self.coordinator.state.get("alarm_lock")
        if val is None:
            return None
        if isinstance(val, int) and 0 <= val < len(_ALARM_LOCK_MAP):
            return _ALARM_LOCK_MAP[val]
        return str(val)

    @property
    def extra_state_attributes(self) -> dict:
        alarm = self.coordinator.state.get("alarm_lock")
        return {"raw_value": alarm} if alarm else {}


_DOOR_STATE_MAP = {
    0: "unknown",
    1: "open",
    2: "closed",
    "unknown": "unknown",
    "open": "open",
    "closed": "closed",
}


class TuyaBLEDoorSensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    _attr_name = "Door"
    _attr_icon = "mdi:door"

    @property
    def unique_id(self):
        return f"{self._mac}_door"

    @property
    def native_value(self) -> str | None:
        val = self.coordinator.state.get("closed_opened")
        if val is None:
            return None
        return _DOOR_STATE_MAP.get(val, str(val))

    @property
    def icon(self) -> str:
        val = self.native_value
        if val == "open":
            return "mdi:door-open"
        if val == "closed":
            return "mdi:door-closed"
        return "mdi:door"


class TuyaBLELastUnlockSensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    _attr_name = "Last unlock"
    _attr_icon = "mdi:key-variant"

    @property
    def unique_id(self):
        return f"{self._mac}_last_unlock"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.state.get("last_unlock_method")

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {}
        user = self.coordinator.state.get("last_unlock_user")
        ts = self.coordinator.state.get("last_unlock_time")
        if user is not None:
            attrs["user_id"] = user
        if ts is not None:
            attrs["timestamp"] = ts
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("last_unlock_method") is None:
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unknown", "unavailable"):
                self.coordinator.state["last_unlock_method"] = last.state


class TuyaBLEDiagnosticSensor(TuyaBLELockEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry, data_key: str, name: str, uid_suffix: str):
        super().__init__(coordinator, entry)
        # Set name *after* super() so the parent's __init__ can't wipe it
        # back to None. Also set translation_key so the UI can localise.
        self._attr_name = name
        self._attr_translation_key = uid_suffix
        self._uid_suffix = uid_suffix
        self._data_key = data_key

    @property
    def unique_id(self):
        return f"{self._mac}_{self._uid_suffix}"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str | None:
        return self.coordinator.device_data.get(self._data_key)
