"""Config flow for Tuya BLE Smart Lock — hub-based architecture.

Creates a single config entry per Tuya cloud account. When new BLE locks
are discovered, they are auto-added to the device store and the entry is
reloaded to pick them up.
"""

from __future__ import annotations

import hashlib
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.components import bluetooth

from .const import (
    DOMAIN,
    CONF_TUYA_EMAIL,
    CONF_TUYA_PASSWORD,
    CONF_TUYA_COUNTRY,
    CONF_TUYA_REGION,
)
from .device_store import DeviceStore
from .tuya_cloud import async_fetch_auth_key

_LOGGER = logging.getLogger(__name__)


def _decrypt_uuid(service_data: bytes, encrypted_id: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = hashlib.md5(service_data).digest()
    dec = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    return (dec.update(encrypted_id) + dec.finalize()).decode("ascii").rstrip("\x00")


STEP_CLOUD_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Required("country_code", description={"suggested_value": "1"}): str,
    vol.Required("region", default="us"): vol.In(["us", "eu", "cn", "in"]),
})


class TuyaBLELockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self):
        self._mac = None
        self._name = None
        self._uuid = None
        self._email = None
        self._password = None
        self._country = None
        self._region = None

    # ---- BLE discovery ----

    async def async_step_bluetooth(self, discovery_info):
        self._mac = discovery_info.address
        self._name = discovery_info.name or self._mac

        # Try decrypt UUID from FD50 service data
        svc_data = None
        for suuid, sd in (discovery_info.service_data or {}).items():
            if "fd50" in suuid.lower():
                svc_data = sd
                break
        man = discovery_info.manufacturer_data.get(0x07D0)
        if svc_data and man and len(man) >= 20:
            try:
                self._uuid = _decrypt_uuid(bytes(svc_data), bytes(man[4:20]))
            except Exception:
                self._uuid = None

        # Check if already known in device store
        existing_entries = self._async_current_entries()
        if existing_entries:
            entry = existing_entries[0]
            device_store = DeviceStore(self.hass)
            await device_store.async_load()
            if device_store.get_device(self._mac):
                return self.async_abort(reason="already_configured")
            # Hub exists — try auto-add using stored creds
            return await self._async_auto_add_device(entry, device_store)

        # No hub entry yet — set unique_id and start cloud login
        await self.async_set_unique_id(self._mac)
        self._abort_if_unique_id_configured()
        return await self.async_step_cloud_login()

    async def _async_auto_add_device(self, entry, device_store):
        """Auto-add a discovered device using the hub's cloud credentials."""
        creds = entry.data
        email = creds.get(CONF_TUYA_EMAIL, "")
        password = creds.get(CONF_TUYA_PASSWORD, "")
        country = creds.get(CONF_TUYA_COUNTRY, "")
        region = creds.get(CONF_TUYA_REGION, "")

        if not email or not password:
            _LOGGER.warning("Hub entry has no cloud credentials, cannot auto-add %s", self._mac)
            return self.async_abort(reason="missing_credentials")

        try:
            cloud_result = await async_fetch_auth_key(
                self.hass, self._uuid or "", email, password,
                country, region, device_mac=self._mac or "",
            )
        except Exception:
            _LOGGER.debug("Auto-add cloud fetch failed for %s", self._mac, exc_info=True)
            return self.async_abort(reason="cloud_fetch_failed")

        auth_key = cloud_result.get("auth_key", "")
        local_key = cloud_result.get("local_key", "")
        device_id = cloud_result.get("device_id", "")
        product_id = cloud_result.get("product_id", "")
        name = cloud_result.get("name") or self._name or self._mac
        uuid = cloud_result.get("uuid") or self._uuid or ""

        if local_key and device_id:
            # Device already bound to Tuya account — derive BLE credentials
            login_key = local_key[:6].encode()
            virtual_id = (device_id.encode() + b"\x00" * 22)[:22]
            await device_store.async_add_device(self._mac, {
                "uuid": uuid,
                "login_key": login_key.hex(),
                "virtual_id": virtual_id.hex(),
                "auth_key": auth_key,
                "product_id": product_id,
                "name": name,
            })
            _LOGGER.info("Auto-added device %s (%s) to hub", name, self._mac)
            # Reload entry to pick up new device
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="device_added")

        # Device not bound — need BLE pairing (show confirm to user)
        self._email = email
        self._password = password
        self._country = country
        self._region = region
        return await self.async_step_confirm_new_device()

    async def async_step_confirm_new_device(self, user_input=None):
        """Confirm adding a new lock that needs BLE pairing."""
        if user_input is not None:
            # TODO: BLE pairing for unbound devices under hub
            # For now, this path is rare — most devices are already in the Tuya app
            return self.async_abort(reason="pairing_not_implemented")
        return self.async_show_form(
            step_id="confirm_new_device",
            description_placeholders={"name": self._name, "mac": self._mac},
        )

    # ---- Manual setup (first hub creation) ----

    async def async_step_user(self, user_input=None):
        """Manual setup entry point."""
        # If hub already exists, abort
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return await self.async_step_cloud_login()

    async def async_step_cloud_login(self, user_input=None):
        if user_input:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            self._country = user_input["country_code"]
            self._region = user_input["region"]

            # Validate credentials by attempting a cloud call
            try:
                if self._mac:
                    cloud_result = await async_fetch_auth_key(
                        self.hass, self._uuid or "", self._email,
                        self._password, self._country, self._region,
                        device_mac=self._mac or "",
                    )
                    # If we got device data, store it
                    return await self._create_hub_with_device(cloud_result)
                else:
                    # No device discovered yet — just create hub with creds
                    return await self._create_hub_entry()
            except Exception:
                _LOGGER.exception("Cloud login failed")
                return self.async_show_form(
                    step_id="cloud_login",
                    data_schema=STEP_CLOUD_SCHEMA,
                    errors={"base": "auth_key_failed"},
                )

        return self.async_show_form(
            step_id="cloud_login",
            data_schema=STEP_CLOUD_SCHEMA,
        )

    async def _create_hub_with_device(self, cloud_result: dict):
        """Create the hub entry and add the first device."""
        auth_key = cloud_result.get("auth_key", "")
        local_key = cloud_result.get("local_key", "")
        device_id = cloud_result.get("device_id", "")
        product_id = cloud_result.get("product_id", "")
        name = cloud_result.get("name") or self._name or self._mac
        uuid = cloud_result.get("uuid") or self._uuid or ""

        # Save device to store
        device_store = DeviceStore(self.hass)
        await device_store.async_load()

        if local_key and device_id:
            login_key = local_key[:6].encode()
            virtual_id = (device_id.encode() + b"\x00" * 22)[:22]
            await device_store.async_add_device(self._mac, {
                "uuid": uuid,
                "login_key": login_key.hex(),
                "virtual_id": virtual_id.hex(),
                "auth_key": auth_key,
                "product_id": product_id,
                "name": name,
            })

        return await self._create_hub_entry()

    async def _create_hub_entry(self):
        """Create the single hub config entry with cloud credentials."""
        await self.async_set_unique_id(self._email)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Tuya BLE Locks",
            data={
                CONF_TUYA_EMAIL: self._email,
                CONF_TUYA_PASSWORD: self._password,
                CONF_TUYA_COUNTRY: self._country,
                CONF_TUYA_REGION: self._region,
            },
        )

    # ---- Reauth ----

    async def async_step_reauth(self, user_input=None):
        return await self.async_step_cloud_login()
