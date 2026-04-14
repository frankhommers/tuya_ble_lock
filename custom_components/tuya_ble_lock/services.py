"""Service handlers for Tuya BLE lock integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    CRED_PASSWORD,
    CRED_FINGERPRINT,
    CRED_CARD,
    STAGE_NAMES,
)
from .credential_store import CredentialStore
from .ble_commands import (
    build_enroll_payload,
    build_delete_payload,
    build_temp_password_payload,
    parse_enroll_response,
    SYNC_MARKER,
)
from .models import TuyaBLELockData

_LOGGER = logging.getLogger(__name__)

ADD_PIN_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("person"): vol.Any(str, None),
    vol.Optional("member_name"): vol.Any(str, None),
    vol.Required("pin_code"): str,
    vol.Optional("admin", default=False): bool,
})

ADD_FINGERPRINT_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("person"): vol.Any(str, None),
    vol.Optional("member_name"): vol.Any(str, None),
    vol.Optional("finger"): vol.Any(str, None),
    vol.Optional("admin", default=False): bool,
})

ADD_CARD_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("person"): vol.Any(str, None),
    vol.Optional("member_name"): vol.Any(str, None),
    vol.Optional("admin", default=False): bool,
})

CRED_TYPE_NAMES = {"pin": CRED_PASSWORD, "card": CRED_CARD, "fingerprint": CRED_FINGERPRINT}
CRED_TYPE_LABELS = {CRED_PASSWORD: "pin", CRED_CARD: "card", CRED_FINGERPRINT: "fingerprint", 0x04: "face"}

DELETE_CREDENTIAL_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("credential_id"): str,
    vol.Optional("person"): vol.Any(str, None),
    vol.Optional("member_name"): vol.Any(str, None),
    vol.Optional("cred_type"): vol.Any("pin", "card", "fingerprint", None),
})

LIST_CREDENTIALS_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
})

CREATE_TEMP_PASSWORD_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Required("name"): str,
    vol.Required("pin_code"): str,
    vol.Required("effective_time"): str,
    vol.Required("expiry_time"): str,
})


def _resolve_member_name(hass: HomeAssistant, call_data: dict) -> str:
    """Resolve person entity or member_name to a friendly name."""
    person_entity_id = call_data.get("person")
    if person_entity_id:
        state = hass.states.get(person_entity_id)
        if state:
            return state.name
        return person_entity_id.split(".")[-1].replace("_", " ").title()
    name = call_data.get("member_name")
    if name:
        return name
    return "Member"


def _get_coordinator(hass: HomeAssistant, device_id: str):
    """Resolve device_id to (mac, coordinator).

    Accepts MAC address, HA device registry ID, or config entry ID.
    """
    # Check all coordinators by MAC
    for entry in hass.config_entries.async_entries(DOMAIN):
        rd: TuyaBLELockData | None = getattr(entry, "runtime_data", None)
        if not rd or not rd.coordinators:
            continue
        # Direct MAC match
        mac_upper = device_id.upper()
        if mac_upper in rd.coordinators:
            return mac_upper, rd.coordinators[mac_upper]

    # Try device registry lookup
    from homeassistant.helpers import device_registry as dr
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if device:
        for ident in device.identifiers:
            if ident[0] == DOMAIN:
                mac = ident[1]
                for entry in hass.config_entries.async_entries(DOMAIN):
                    rd = getattr(entry, "runtime_data", None)
                    if rd and mac in rd.coordinators:
                        return mac, rd.coordinators[mac]

    raise HomeAssistantError(
        f"Device not found: '{device_id}'. Use MAC address or HA device ID."
    )


def _get_service_dp(coordinator, service_name: str) -> int | None:
    profile = coordinator.profile or {}
    svc_cfg = profile.get("services", {}).get(service_name)
    if svc_cfg:
        return svc_cfg.get("dp")
    return None


def _get_sync_dp(coordinator, service_name: str) -> int | None:
    profile = coordinator.profile or {}
    svc_cfg = profile.get("services", {}).get(service_name)
    if svc_cfg:
        return svc_cfg.get("sync_dp")
    return None


async def async_register_services(hass: HomeAssistant) -> None:

    async def handle_add_pin(call: ServiceCall) -> None:
        device_ids = call.data["device_id"]
        member_name = _resolve_member_name(hass, call.data)
        pin_code = call.data["pin_code"]
        admin = call.data.get("admin", False)

        if not pin_code.isdigit() or len(pin_code) < 6:
            raise HomeAssistantError("PIN must be at least 6 digits")

        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        member = store.get_member_by_name(member_name)
        if not member:
            member = await store.async_add_member(member_name)

        if not isinstance(device_ids, list):
            device_ids = [device_ids]
        for device_id in device_ids:
            mac, coordinator = _get_coordinator(hass, device_id)
            dp_create = _get_service_dp(coordinator, "add_pin")
            if dp_create is None:
                raise HomeAssistantError("add_pin service not supported by this device profile")
            try:
                await coordinator._async_ensure_connected()
                pin_bytes = [int(d) for d in pin_code]
                payload = build_enroll_payload(
                    CRED_PASSWORD, member.member_id, admin=admin, password_digits=pin_bytes
                )
                result = await coordinator._session.async_send_dp_raw(
                    dp_create, payload
                )
                if result:
                    resp = parse_enroll_response(result["raw"])
                    if resp.get("stage") == "COMPLETE" and resp.get("result") == "OK":
                        await store.async_add_credential(
                            member_id=member.member_id,
                            lock_entry_id=mac,
                            cred_type=CRED_PASSWORD,
                            hw_id=resp.get("hw_id", 0),
                            name=f"{member_name} PIN",
                        )
                    else:
                        raise HomeAssistantError(f"PIN enrollment failed: {resp}")
                else:
                    raise HomeAssistantError("No response from lock during PIN enrollment")
            finally:
                await coordinator._session.async_disconnect()

    async def handle_add_fingerprint(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        member_name = _resolve_member_name(hass, call.data)
        admin = call.data.get("admin", False)
        finger = (call.data.get("finger") or "").strip() or None

        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        member = store.get_member_by_name(member_name)
        if not member:
            member = await store.async_add_member(member_name)

        mac, coordinator = _get_coordinator(hass, device_id)
        dp_create = _get_service_dp(coordinator, "add_fingerprint")
        if dp_create is None:
            raise HomeAssistantError("add_fingerprint service not supported by this device profile")
        sync_dp = _get_sync_dp(coordinator, "add_fingerprint")
        try:
            await coordinator._async_ensure_connected()

            if sync_dp:
                await coordinator._session.async_send_dp_raw(sync_dp, SYNC_MARKER)

            payload = build_enroll_payload(CRED_FINGERPRINT, member.member_id, admin=admin)
            results = await coordinator._session.async_send_dp_raw_long(
                dp_create, payload, timeout=60.0
            )

            for dp in results:
                if dp["id"] == dp_create and dp["type"] == 0 and len(dp["raw"]) >= 7:
                    resp = parse_enroll_response(dp["raw"])
                    if resp.get("stage") == "COMPLETE" and resp.get("result") == "OK":
                        cred_name = (
                            f"{member_name} {finger}" if finger
                            else f"{member_name} Fingerprint"
                        )
                        await store.async_add_credential(
                            member_id=member.member_id,
                            lock_entry_id=mac,
                            cred_type=CRED_FINGERPRINT,
                            hw_id=resp.get("hw_id", 0),
                            name=cred_name,
                        )
                        return
                    elif resp.get("stage") in ("FAILED", "CANCELLED"):
                        raise HomeAssistantError(f"Fingerprint enrollment failed: {resp}")

            raise HomeAssistantError("Fingerprint enrollment timed out (no completion in 60s)")
        finally:
            await coordinator._session.async_disconnect()

    async def handle_add_card(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        member_name = _resolve_member_name(hass, call.data)
        admin = call.data.get("admin", False)

        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        member = store.get_member_by_name(member_name)
        if not member:
            member = await store.async_add_member(member_name)

        mac, coordinator = _get_coordinator(hass, device_id)
        dp_create = _get_service_dp(coordinator, "add_card")
        if dp_create is None:
            raise HomeAssistantError("add_card service not supported by this device profile")
        sync_dp = _get_sync_dp(coordinator, "add_card")
        try:
            await coordinator._async_ensure_connected()

            if sync_dp:
                await coordinator._session.async_send_dp_raw(sync_dp, SYNC_MARKER)

            payload = build_enroll_payload(CRED_CARD, member.member_id, admin=admin)
            results = await coordinator._session.async_send_dp_raw_long(
                dp_create, payload, timeout=30.0
            )

            for dp in results:
                if dp["id"] == dp_create and dp["type"] == 0 and len(dp["raw"]) >= 7:
                    resp = parse_enroll_response(dp["raw"])
                    if resp.get("stage") == "COMPLETE" and resp.get("result") == "OK":
                        await store.async_add_credential(
                            member_id=member.member_id,
                            lock_entry_id=mac,
                            cred_type=CRED_CARD,
                            hw_id=resp.get("hw_id", 0),
                            name=f"{member_name} Card",
                        )
                        return
                    elif resp.get("stage") in ("FAILED", "CANCELLED"):
                        raise HomeAssistantError(f"Card enrollment failed: {resp}")

            raise HomeAssistantError("Card enrollment timed out (no completion in 30s)")
        finally:
            await coordinator._session.async_disconnect()

    async def handle_delete_credential(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        credential_id = call.data.get("credential_id")

        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        mac, coordinator = _get_coordinator(hass, device_id)
        dp_delete = _get_service_dp(coordinator, "delete_credential")
        if dp_delete is None:
            raise HomeAssistantError("delete_credential service not supported by this device profile")

        if credential_id:
            cred_data = store._data["credentials"].get(credential_id)
            if not cred_data:
                raise HomeAssistantError(f"Credential '{credential_id}' not found")
            creds_to_delete = [(credential_id, cred_data)]
        else:
            member_name = _resolve_member_name(hass, call.data)
            member = store.get_member_by_name(member_name)
            if not member:
                raise HomeAssistantError(f"Member '{member_name}' not found")

            cred_type_filter = None
            if call.data.get("cred_type"):
                cred_type_filter = CRED_TYPE_NAMES[call.data["cred_type"]]

            creds_to_delete = [
                (cid, c) for cid, c in store._data["credentials"].items()
                if c["member_id"] == member.member_id
                and c["lock_entry_id"] == mac
                and (cred_type_filter is None or c["cred_type"] == cred_type_filter)
            ]
            if not creds_to_delete:
                ctype = call.data.get("cred_type", "any")
                raise HomeAssistantError(
                    f"No {ctype} credentials found for '{member_name}' on this lock"
                )

        try:
            await coordinator._async_ensure_connected()

            for cid, cred_data in creds_to_delete:
                delete_payload = build_delete_payload(
                    cred_type=cred_data["cred_type"],
                    member_id=cred_data["member_id"],
                    hw_id=cred_data["hw_id"],
                )
                result = await coordinator._session.async_send_dp_raw(
                    dp_delete, delete_payload
                )
                _LOGGER.info("Delete credential %s result: %s", cred_data.get("name", cid), result)
                await store.async_delete_credential(cid)
        finally:
            await coordinator._session.async_disconnect()

    async def handle_create_temp_password(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        name = call.data["name"]
        pin_code = call.data["pin_code"]
        effective_time = call.data["effective_time"]
        expiry_time = call.data["expiry_time"]

        if not pin_code.isdigit() or len(pin_code) < 6:
            raise HomeAssistantError("PIN must be at least 6 digits")

        mac, coordinator = _get_coordinator(hass, device_id)
        dp_temp = _get_service_dp(coordinator, "create_temp_password")
        if dp_temp is None:
            raise HomeAssistantError("create_temp_password service not supported by this device profile")
        try:
            await coordinator._async_ensure_connected()

            store: CredentialStore = hass.data[DOMAIN]["credential_store"]

            from datetime import datetime
            eff_ts = int(datetime.fromisoformat(effective_time).timestamp())
            exp_ts = int(datetime.fromisoformat(expiry_time).timestamp())

            pin_bytes = [int(d) for d in pin_code]
            payload = build_temp_password_payload(pin_bytes, name, eff_ts, exp_ts)
            result = await coordinator._session.async_send_dp_raw(
                dp_temp, payload
            )
            if result:
                await store.async_add_temp_password(
                    lock_entry_id=mac,
                    name=name,
                    effective=eff_ts,
                    expiry=exp_ts,
                )
            else:
                raise HomeAssistantError("No response from lock for temp password creation")
        finally:
            await coordinator._session.async_disconnect()

    async def handle_list_credentials(call: ServiceCall):
        device_id = call.data["device_id"]
        mac, coordinator = _get_coordinator(hass, device_id)
        store: CredentialStore = hass.data[DOMAIN]["credential_store"]

        creds = store.get_credentials_for_lock(mac)
        members = {m.member_id: m.name for m in store.get_members()}

        result = []
        for c in creds:
            result.append({
                "credential_id": c.credential_id,
                "member": members.get(c.member_id, f"member_{c.member_id}"),
                "type": CRED_TYPE_LABELS.get(c.cred_type, f"unknown_{c.cred_type}"),
                "name": c.name,
                "hw_id": c.hw_id,
            })
        return {"credentials": result}

    hass.services.async_register(DOMAIN, "add_pin", handle_add_pin, schema=ADD_PIN_SCHEMA)
    hass.services.async_register(DOMAIN, "add_fingerprint", handle_add_fingerprint, schema=ADD_FINGERPRINT_SCHEMA)
    hass.services.async_register(DOMAIN, "add_card", handle_add_card, schema=ADD_CARD_SCHEMA)
    hass.services.async_register(DOMAIN, "delete_credential", handle_delete_credential, schema=DELETE_CREDENTIAL_SCHEMA)
    hass.services.async_register(DOMAIN, "list_credentials", handle_list_credentials, schema=LIST_CREDENTIALS_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "create_temp_password", handle_create_temp_password, schema=CREATE_TEMP_PASSWORD_SCHEMA)
