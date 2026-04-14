"""Tuya BLE Smart Lock integration — hub-based architecture.

One config entry per Tuya cloud account. All locks are devices under that hub.
Per-device BLE credentials are stored in a separate DeviceStore.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components import bluetooth
from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    CONF_TUYA_EMAIL,
    CONF_TUYA_PASSWORD,
    CONF_TUYA_COUNTRY,
    CONF_TUYA_REGION,
)
from .credential_store import CredentialStore
from .device_store import DeviceStore
from .device_profiles import async_load_profile
from .models import TuyaBLELockData

_LOGGER = logging.getLogger(__name__)

# All platforms — switch is always loaded (persistent connection switch)
ALL_PLATFORMS = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
]


def _platforms_for_devices(profiles: dict[str, dict]) -> list[Platform]:
    """Determine which platforms to load based on all device profiles."""
    platforms = {Platform.LOCK, Platform.SENSOR, Platform.BUTTON, Platform.SWITCH}
    for profile in profiles.values():
        entities = profile.get("entities", {})
        select_keys = ("volume_select", "language_select", "unlock_mode_select")
        if any(k in entities for k in select_keys):
            platforms.add(Platform.SELECT)
        if "auto_lock_time_number" in entities:
            platforms.add(Platform.NUMBER)
    return list(platforms)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    from .services import async_register_services

    # Migrate legacy per-device entries (v1) to hub model (v2)
    await _async_migrate_legacy_entries(hass)

    await async_register_services(hass)
    return True


async def _async_migrate_legacy_entries(hass: HomeAssistant) -> None:
    """Merge old per-device config entries into a single hub entry."""
    entries = hass.config_entries.async_entries(DOMAIN)
    legacy = [e for e in entries if e.version < 2]
    if not legacy:
        return

    _LOGGER.info("Migrating %d legacy config entries to hub model", len(legacy))

    # Load or create device store
    device_store = DeviceStore(hass)
    await device_store.async_load()

    # Collect cloud creds from first entry that has them
    cloud_creds = {}
    for entry in legacy:
        opts = entry.options or {}
        if opts.get(CONF_TUYA_EMAIL):
            cloud_creds = {
                CONF_TUYA_EMAIL: opts[CONF_TUYA_EMAIL],
                CONF_TUYA_PASSWORD: opts.get(CONF_TUYA_PASSWORD, ""),
                CONF_TUYA_COUNTRY: opts.get(CONF_TUYA_COUNTRY, ""),
                CONF_TUYA_REGION: opts.get(CONF_TUYA_REGION, ""),
            }
            break

    # Move all device data to the store and update credential records
    from .credential_store import CredentialStore
    cred_store = CredentialStore(hass)
    await cred_store.async_load()

    for entry in legacy:
        mac = entry.data.get("device_mac", "").upper()
        if mac and not device_store.get_device(mac):
            await device_store.async_add_device(mac, {
                "uuid": entry.data.get("device_uuid", ""),
                "login_key": entry.data.get("login_key", ""),
                "virtual_id": entry.data.get("virtual_id", ""),
                "auth_key": entry.data.get("auth_key", ""),
                "product_id": entry.data.get("product_id", ""),
                "name": entry.title,
            })
        # Update credential records: old entry_id -> MAC
        for cid, cdata in list(cred_store._data.get("credentials", {}).items()):
            if cdata.get("lock_entry_id") == entry.entry_id:
                cdata["lock_entry_id"] = mac
        for pid, pdata in list(cred_store._data.get("temp_passwords", {}).items()):
            if pdata.get("lock_entry_id") == entry.entry_id:
                pdata["lock_entry_id"] = mac
    await cred_store.async_save()

    # Convert first entry to hub format (v2)
    hub_entry = legacy[0]
    hass.config_entries.async_update_entry(
        hub_entry,
        data=cloud_creds,
        options={},
        version=2,
        unique_id=cloud_creds.get(CONF_TUYA_EMAIL, "tuya_ble_lock_hub"),
        title="Tuya BLE Locks",
    )

    # Remove remaining legacy entries
    for entry in legacy[1:]:
        await hass.config_entries.async_remove(entry.entry_id)

    _LOGGER.info(
        "Migration complete: hub entry=%s, %d devices in store",
        hub_entry.entry_id, len(device_store.devices),
    )


async def _async_backfill_btsc_fields(
    hass: HomeAssistant, entry: ConfigEntry, device_store: DeviceStore
) -> None:
    """Fetch local_key/sec_key/check_code from cloud for devices missing them.

    Devices added before btScyChannel support was merged only have
    login_key (the first 6 bytes of local_key). For protocol-5.0 "new
    security" locks we need the full local_key, plus sec_key and the
    current DP71 check code. This runs once per restart and is a no-op
    once the fields are populated.
    """
    missing = [
        mac for mac, d in device_store.devices.items()
        if not d.get("local_key") or not d.get("sec_key")
    ]
    if not missing:
        return

    email = entry.data.get(CONF_TUYA_EMAIL, "")
    password = entry.data.get(CONF_TUYA_PASSWORD, "")
    country = entry.data.get(CONF_TUYA_COUNTRY, "")
    region = entry.data.get(CONF_TUYA_REGION, "")
    if not (email and password):
        _LOGGER.warning(
            "%d device(s) missing btScyChannel fields but hub has no cloud "
            "credentials to backfill", len(missing),
        )
        return

    from .tuya_cloud import async_fetch_auth_key

    for mac in missing:
        try:
            res = await async_fetch_auth_key(
                hass, "", email, password, country, region, device_mac=mac,
            )
        except Exception as exc:
            _LOGGER.warning("Backfill for %s failed: %s", mac, exc)
            continue
        updates = {
            "local_key": res.get("local_key", ""),
            "sec_key": res.get("sec_key", ""),
            "check_code": res.get("check_code", ""),
        }
        if updates["local_key"] and updates["sec_key"]:
            await device_store.async_update_device(mac, **updates)
            _LOGGER.info(
                "Backfilled btScyChannel fields for %s (check_code=%s)",
                mac, updates["check_code"] or "<empty>",
            )
        else:
            _LOGGER.warning(
                "Backfill for %s incomplete: local_key=%s sec_key=%s",
                mac, bool(updates["local_key"]), bool(updates["sec_key"]),
            )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # Load stores
    device_store = DeviceStore(hass)
    await device_store.async_load()

    if "credential_store" not in hass.data[DOMAIN]:
        cred_store = CredentialStore(hass)
        await cred_store.async_load()
        hass.data[DOMAIN]["credential_store"] = cred_store
    credential_store = hass.data[DOMAIN]["credential_store"]

    # Register hub device in device registry
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Tuya BLE Locks",
        manufacturer="Tuya",
        model="BLE Lock Hub",
    )

    # Create coordinators for each known device
    from .ble_session import TuyaBLELockSession
    from .coordinator import TuyaBLELockCoordinator

    # Auto-migrate: fetch local_key/sec_key/check_code for devices stored
    # before btScyChannel support was added (pre-v2.1 of the integration).
    await _async_backfill_btsc_fields(hass, entry, device_store)

    coordinators: dict[str, TuyaBLELockCoordinator] = {}
    profiles: dict[str, dict] = {}

    for mac, dev_data in device_store.devices.items():
        ble_device = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
        if not ble_device:
            _LOGGER.debug("BLE device %s not available at startup, skipping", mac)
            continue

        login_key = bytes.fromhex(dev_data.get("login_key", ""))
        virtual_id = bytes.fromhex(dev_data.get("virtual_id", ""))
        device_uuid = dev_data.get("uuid", "")
        product_id = dev_data.get("product_id")
        device_name = dev_data.get("name", mac)
        local_key_str = dev_data.get("local_key") or ""
        sec_key_str = dev_data.get("sec_key") or ""
        local_key_b = local_key_str.encode("utf-8") if local_key_str else None
        sec_key_b = sec_key_str.encode("utf-8") if sec_key_str else None

        profile = await async_load_profile(hass, product_id)
        profiles[mac] = profile

        protocol_version = profile.get("protocol_version", 4)
        session = TuyaBLELockSession(
            hass, ble_device, login_key, virtual_id, device_uuid,
            protocol_version=protocol_version,
            local_key=local_key_b,
            sec_key=sec_key_b,
        )

        coordinator = TuyaBLELockCoordinator(
            hass, entry, mac, device_name, dev_data,
            ble_device, session, profile,
        )
        coordinators[mac] = coordinator

        # One-shot status fetch in background
        entry.async_create_background_task(
            hass, coordinator.async_one_shot_status(),
            f"tuya_ble_lock_status_{mac}",
        )

    platforms = _platforms_for_devices(profiles)
    runtime_data = TuyaBLELockData(
        device_store=device_store,
        credential_store=credential_store,
        coordinators=coordinators,
        platforms=platforms,
    )
    entry.runtime_data = runtime_data
    hass.data[DOMAIN]["device_store"] = device_store

    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data: TuyaBLELockData = entry.runtime_data
    for coordinator in data.coordinators.values():
        coordinator._stopping = True
        coordinator._persistent_connection = False
        if coordinator._keepalive_task and not coordinator._keepalive_task.done():
            coordinator._keepalive_task.cancel()
        if coordinator._idle_timer is not None:
            coordinator._idle_timer.cancel()
        await coordinator._session.async_disconnect()
    return await hass.config_entries.async_unload_platforms(entry, data.platforms)
