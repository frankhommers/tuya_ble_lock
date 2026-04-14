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

# Country → (country_code, region, display_name)
# Mappings from https://developer.tuya.com/en/docs/iot/oem-app-data-center-distributed
COUNTRY_OPTIONS: dict[str, tuple[str, str, str]] = {
    # Western America Data Center
    "us": ("1", "us", "United States"),
    "ca": ("1", "us", "Canada"),
    # Eastern America Data Center (mapped to "us" region API)
    "mx": ("52", "us", "México"),
    "br": ("55", "us", "Brasil"),
    "ar": ("54", "us", "Argentina"),
    "cl": ("56", "us", "Chile"),
    "co": ("57", "us", "Colombia"),
    "pe": ("51", "us", "Perú"),
    "ec": ("593", "us", "Ecuador"),
    "nz": ("64", "us", "New Zealand"),
    # Central Europe Data Center
    "nl": ("31", "eu", "Nederland"),
    "be": ("32", "eu", "België"),
    "de": ("49", "eu", "Deutschland"),
    "fr": ("33", "eu", "France"),
    "gb": ("44", "eu", "United Kingdom"),
    "es": ("34", "eu", "España"),
    "it": ("39", "eu", "Italia"),
    "pt": ("351", "eu", "Portugal"),
    "at": ("43", "eu", "Österreich"),
    "ch": ("41", "eu", "Schweiz"),
    "se": ("46", "eu", "Sverige"),
    "no": ("47", "eu", "Norge"),
    "dk": ("45", "eu", "Danmark"),
    "fi": ("358", "eu", "Suomi"),
    "pl": ("48", "eu", "Polska"),
    "ie": ("353", "eu", "Ireland"),
    "cz": ("420", "eu", "Česko"),
    "ro": ("40", "eu", "România"),
    "hu": ("36", "eu", "Magyarország"),
    "gr": ("30", "eu", "Ελλάδα"),
    "tr": ("90", "eu", "Türkiye"),
    "il": ("972", "eu", "Israel"),
    "za": ("27", "eu", "South Africa"),
    "au": ("61", "eu", "Australia"),
    "ru": ("7", "eu", "Russia"),
    "ua": ("380", "eu", "Ukraine"),
    "eg": ("20", "eu", "Egypt"),
    "ng": ("234", "eu", "Nigeria"),
    "ke": ("254", "eu", "Kenya"),
    "pk": ("92", "eu", "Pakistan"),
    "bd": ("880", "eu", "Bangladesh"),
    "lk": ("94", "eu", "Sri Lanka"),
    "np": ("977", "eu", "Nepal"),
    "sa": ("966", "eu", "Saudi Arabia"),
    "ae": ("971", "eu", "United Arab Emirates"),
    "qa": ("974", "eu", "Qatar"),
    "kw": ("965", "eu", "Kuwait"),
    "jp": ("81", "eu", "日本"),
    "kr": ("82", "eu", "대한민국"),
    "mn": ("976", "eu", "Mongolia"),
    # Singapore Data Center
    "sg": ("65", "eu", "Singapore"),
    "my": ("60", "eu", "Malaysia"),
    "th": ("66", "eu", "Thailand"),
    "id": ("62", "eu", "Indonesia"),
    "ph": ("63", "eu", "Philippines"),
    "vn": ("84", "eu", "Việt Nam"),
    "mm": ("95", "eu", "Myanmar"),
    "kh": ("855", "eu", "Cambodia"),
    "la": ("856", "eu", "Laos"),
    "bn": ("673", "eu", "Brunei"),
    "hk": ("852", "eu", "Hong Kong"),
    "tw": ("886", "eu", "Taiwan"),
    # India Data Center
    "in": ("91", "in", "India"),
    # China Data Center
    "cn": ("86", "cn", "中国"),
    "other": ("", "", "Other (manual)"),
}

# Region dropdown options (display_name → api_value)
REGION_OPTIONS: dict[str, str] = {
    "Europe (EU)": "eu",
    "Americas (US)": "us",
    "China (CN)": "cn",
    "India (IN)": "in",
}


def _country_choices() -> dict[str, str]:
    """Sorted country choices for dropdown."""
    return {k: v[2] for k, v in sorted(COUNTRY_OPTIONS.items(), key=lambda x: x[1][2])}


def _build_cloud_schema(selected_country: str | None = None) -> vol.Schema:
    """Build the login schema based on selected country."""
    country = selected_country or "nl"
    info = COUNTRY_OPTIONS.get(country, ("", "eu", ""))

    # If "other", show manual country code + region dropdown
    if country == "other":
        return vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required("country_code"): str,
                vol.Required("region", default="Europe (EU)"): vol.In(
                    list(REGION_OPTIONS.keys())
                ),
            }
        )

    # Normal country — region is auto-detected, only email + password needed
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


def _decrypt_uuid(service_data: bytes, encrypted_id: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.md5(service_data).digest()
    dec = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    return (dec.update(encrypted_id) + dec.finalize()).decode("ascii").rstrip("\x00")


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
        self._selected_country = None

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
        return await self.async_step_select_country()

    async def _async_auto_add_device(self, entry, device_store):
        """Auto-add a discovered device using the hub's cloud credentials."""
        creds = entry.data
        email = creds.get(CONF_TUYA_EMAIL, "")
        password = creds.get(CONF_TUYA_PASSWORD, "")
        country = creds.get(CONF_TUYA_COUNTRY, "")
        region = creds.get(CONF_TUYA_REGION, "")

        if not email or not password:
            _LOGGER.warning(
                "Hub entry has no cloud credentials, cannot auto-add %s", self._mac
            )
            return self.async_abort(reason="missing_credentials")

        try:
            cloud_result = await async_fetch_auth_key(
                self.hass,
                self._uuid or "",
                email,
                password,
                country,
                region,
                device_mac=self._mac or "",
            )
        except Exception:
            _LOGGER.debug(
                "Auto-add cloud fetch failed for %s", self._mac, exc_info=True
            )
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
            await device_store.async_add_device(
                self._mac,
                {
                    "uuid": uuid,
                    "login_key": login_key.hex(),
                    "virtual_id": virtual_id.hex(),
                    "auth_key": auth_key,
                    "product_id": product_id,
                    "name": name,
                    "local_key": local_key,
                    "sec_key": cloud_result.get("sec_key", ""),
                    "check_code": cloud_result.get("check_code", ""),
                    "cloud_dps": cloud_result.get("dps") or {},
                },
            )
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
        return await self.async_step_select_country()

    # ---- Step 1: Country selection ----

    async def async_step_select_country(self, user_input=None):
        """Select your country to auto-configure region and country code."""
        errors = {}

        if user_input:
            self._selected_country = user_input["country"]
            info = COUNTRY_OPTIONS.get(self._selected_country)

            if self._selected_country == "other":
                return await self.async_step_cloud_login()

            if info:
                self._country = info[0]
                self._region = info[1]
                return await self.async_step_cloud_login()

            errors["base"] = "invalid_country"

        return self.async_show_form(
            step_id="select_country",
            data_schema=vol.Schema(
                {
                    vol.Required("country", default="nl"): vol.In(_country_choices()),
                }
            ),
            errors=errors,
        )

    # ---- Step 2: Login ----

    async def async_step_cloud_login(self, user_input=None):
        """Enter Tuya credentials. Region is auto-filled or shown as dropdown for 'Other'."""
        errors = {}

        if user_input:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]

            # Handle "other" country — resolve region dropdown
            if self._selected_country == "other":
                self._country = user_input.get("country_code", "")
                region_display = user_input.get("region", "Europe (EU)")
                self._region = REGION_OPTIONS.get(region_display, "eu")

            # Validate credentials by attempting a cloud call
            try:
                if self._mac:
                    cloud_result = await async_fetch_auth_key(
                        self.hass,
                        self._uuid or "",
                        self._email,
                        self._password,
                        self._country,
                        self._region,
                        device_mac=self._mac or "",
                    )
                    return await self._create_hub_with_device(cloud_result)
                else:
                    return await self._create_hub_entry()
            except Exception:
                _LOGGER.warning("Cloud login failed", exc_info=True)
                errors["base"] = "auth_key_failed"

        schema = _build_cloud_schema(self._selected_country)
        country_name = COUNTRY_OPTIONS.get(
            self._selected_country or "nl", ("", "", "")
        )[2]
        return self.async_show_form(
            step_id="cloud_login",
            data_schema=schema,
            errors=errors,
            description_placeholders={"country_name": country_name},
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
            await device_store.async_add_device(
                self._mac,
                {
                    "uuid": uuid,
                    "login_key": login_key.hex(),
                    "virtual_id": virtual_id.hex(),
                    "auth_key": auth_key,
                    "product_id": product_id,
                    "name": name,
                    "local_key": local_key,
                    "sec_key": cloud_result.get("sec_key", ""),
                    "check_code": cloud_result.get("check_code", ""),
                    "cloud_dps": cloud_result.get("dps") or {},
                },
            )

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

    # ---- Reauth / Reconfigure ----
    #
    # HA exposes two entry points that do almost the same thing:
    #   * async_step_reauth         — triggered automatically when a call
    #                                 raises ConfigEntryAuthFailed
    #   * async_step_reconfigure    — manual "Reconfigure" option in the
    #                                 hub's ⋮ menu (HA 2024.11+)
    # Both land on the same confirm form.

    async def _prime_from_entry(self) -> None:
        entry = self._reconfigure_source_entry()
        data = (entry.data if entry else {}) or {}
        self._email = data.get(CONF_TUYA_EMAIL, "")
        self._country = data.get(CONF_TUYA_COUNTRY, "")
        self._region = data.get(CONF_TUYA_REGION, "")

    def _reconfigure_source_entry(self):
        entry_id = self.context.get("entry_id", "")
        if entry_id:
            return self.hass.config_entries.async_get_entry(entry_id)
        return None

    async def async_step_reauth(self, entry_data: dict):
        """Automatic reauth (e.g. password invalidated)."""
        self._email = entry_data.get(CONF_TUYA_EMAIL, "")
        self._country = entry_data.get(CONF_TUYA_COUNTRY, "")
        self._region = entry_data.get(CONF_TUYA_REGION, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reconfigure(self, user_input=None):
        """Manual 'Reconfigure' entry (HA hub ⋮ menu)."""
        await self._prime_from_entry()
        return await self.async_step_reauth_confirm(user_input)

    async def async_step_reauth_confirm(self, user_input=None):
        """Ask for password (and country/region if missing), re-login, refresh
        BLE credentials for every device in the store.

        Use cases:
          * Tuya password changed.
          * Lock was re-paired in the Tuya app: localKey/secKey/check_code
            all rotated and need to be pulled down again.
        """
        from .tuya_cloud import async_refresh_all_devices
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id", "")
        )

        if user_input:
            password = user_input[CONF_PASSWORD]
            country = user_input.get(CONF_TUYA_COUNTRY) or self._country
            region = user_input.get(CONF_TUYA_REGION) or self._region
            # Temporarily patch country/region if the user had to supply them
            if entry and (country != entry.data.get(CONF_TUYA_COUNTRY) or
                          region != entry.data.get(CONF_TUYA_REGION)):
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_TUYA_COUNTRY: country,
                          CONF_TUYA_REGION: region},
                )
            try:
                refreshed = await async_refresh_all_devices(
                    self.hass, entry, new_password=password,
                ) if entry else 0
            except Exception as exc:
                _LOGGER.warning("Reauth refresh failed: %s", exc)
                errors["base"] = "auth_key_failed"
            else:
                _LOGGER.info("Reauth complete: refreshed %d device(s)", refreshed)
                if entry:
                    await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        # Build schema: password always, country/region only if missing
        fields: dict = {vol.Required(CONF_PASSWORD): str}
        if not self._country:
            fields[vol.Required(CONF_TUYA_COUNTRY, default="31")] = str
        if not self._region:
            fields[vol.Required(CONF_TUYA_REGION, default="eu")] = vol.In(
                list(REGION_OPTIONS.values())
            )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )
