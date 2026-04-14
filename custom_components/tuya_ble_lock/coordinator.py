"""DataUpdateCoordinator for Tuya BLE lock."""

from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
from datetime import timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .device_profiles import parse_dp_value

_LOGGER = logging.getLogger(__name__)

# Check code — SYD8811 does NOT validate, H8 Pro rejects all-zeros.
DEFAULT_CHECK_CODE = b"12345678"

# Keep BLE connection alive for this long after last operation
IDLE_DISCONNECT_SECONDS = 60

# Cooldown: don't retry connection if last failure was within this window
CONNECT_COOLDOWN_SECONDS = 600  # 10 minutes


class TuyaBLELockCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        mac: str,
        device_name: str,
        device_data: dict,
        ble_device,
        session,
        profile: dict,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name=f"Tuya BLE Lock {device_name}",
            update_interval=timedelta(hours=12),
        )
        self._entry = entry
        self._mac = mac
        self._device_name = device_name
        self._device_data = device_data
        self._session = session
        self._ble_device = ble_device
        self._op_lock = asyncio.Lock()
        self._profile = profile
        self._idle_timer: asyncio.TimerHandle | None = None
        self._listener_task: asyncio.Task | None = None
        self._persistent_connection: bool = False
        self._keepalive_task: asyncio.Task | None = None
        self._last_connect_failure: float = 0.0  # monotonic timestamp
        self._stopping: bool = False

        # Listen for HA shutdown to cancel background tasks
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_ha_stop)

        # Build state dict from profile's state_map
        self.state: dict[str, Any] = {}
        for dp_str, mapping in profile.get("state_map", {}).items():
            key = mapping.get("key", "")
            if key and key != "_ignore" and key not in self.state:
                self.state[key] = None

        # Register push callback so DP reports update state in real-time
        self._session.set_dp_report_callback(self._process_dp_reports)

    @property
    def mac(self) -> str:
        return self._mac

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def device_data(self) -> dict:
        return self._device_data

    @property
    def profile(self) -> dict:
        return self._profile

    # DP id → last_unlock method label. Any DP here that receives a non-zero
    # user id updates self.state['last_unlock_method'] and 'last_unlock_user'.
    _UNLOCK_METHOD_DPS = {
        12: "fingerprint", 13: "password", 14: "dynamic_code",
        15: "card", 16: "mechanical_key", 19: "bluetooth",
        55: "temporary_code", 62: "remote_phone", 63: "remote_voice",
        67: "offline_code",
    }

    def _process_dp_reports(self, dps: list[dict]) -> None:
        """Update state from DP reports using profile's state_map."""
        _LOGGER.debug("Processing %d DPs: %s", len(dps),
                        [(dp["id"], dp["raw"].hex()) for dp in dps])
        state_map = self._profile.get("state_map", {})
        changed = False
        for dp in dps:
            dp_id = dp["id"]
            dp_id_str = str(dp_id)

            # Track last unlock source (any unlock-method DP with non-zero user id)
            if dp_id in self._UNLOCK_METHOD_DPS:
                raw = dp["raw"]
                user_id = int.from_bytes(raw, "big") if raw else 0
                if user_id:
                    self.state["last_unlock_method"] = self._UNLOCK_METHOD_DPS[dp_id]
                    self.state["last_unlock_user"] = user_id
                    self.state["last_unlock_time"] = time.time()
                    changed = True

            mapping = state_map.get(dp_id_str)
            if not mapping:
                continue
            key = mapping.get("key", "")
            parse_type = mapping.get("parse", "raw_byte")
            if not key or key == "_ignore" or parse_type == "ignore":
                continue
            new_val = parse_dp_value(dp["raw"], parse_type)
            if self.state.get(key) != new_val:
                self.state[key] = new_val
                changed = True
        if changed:
            self.async_set_updated_data(self.state)

    def _reset_idle_timer(self) -> None:
        """Reset the idle disconnect timer. Call after every operation."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if not self._persistent_connection:
            loop = self.hass.loop
            self._idle_timer = loop.call_later(
                IDLE_DISCONNECT_SECONDS, lambda: asyncio.ensure_future(self._idle_disconnect())
            )
        # Start background listener if not already running
        self._start_listener()

    def _start_listener(self) -> None:
        """Start background task that processes incoming BLE notifications."""
        if self._listener_task and not self._listener_task.done():
            return
        self._listener_task = self.hass.async_create_task(self._notification_listener())

    async def _notification_listener(self) -> None:
        """Periodically drain notification buffer while BLE is connected.

        This catches unsolicited DP pushes (auto-lock motor_state, physical
        lock/unlock events, etc.) that arrive between explicit operations.
        """
        _LOGGER.debug("Notification listener started")
        try:
            while self._session.is_connected:
                await asyncio.sleep(2.0)
                if not self._session.is_connected:
                    break
                # Only drain if no operation is in progress (don't steal their data)
                if self._op_lock.locked():
                    continue
                if self._session._notif_buf:
                    async with self._session._lock:
                        raw = list(self._session._notif_buf)
                        self._session._notif_buf.clear()
                    if raw:
                        from .ble_protocol import parse_frames
                        frames = parse_frames(self._session._keys, raw)
                        if frames:
                            _LOGGER.debug("Listener: %d frames from %d notifications",
                                            len(frames), len(raw))
                            self._session._dispatch_dp_reports(frames)
        except Exception as exc:
            _LOGGER.debug("Notification listener error: %s", exc)
        _LOGGER.debug("Notification listener stopped")

    async def _idle_disconnect(self) -> None:
        """Disconnect after idle timeout."""
        self._idle_timer = None
        if self._persistent_connection:
            return  # persistent mode — don't disconnect
        if self._session.is_connected:
            _LOGGER.debug("Idle timeout (%ds), disconnecting BLE", IDLE_DISCONNECT_SECONDS)
            await self._session.async_disconnect()
        # Listener will exit on its own when is_connected becomes False

    @property
    def persistent_connection(self) -> bool:
        return self._persistent_connection

    async def _on_ha_stop(self, event) -> None:
        """Cancel background tasks on HA shutdown."""
        self._stopping = True
        self._persistent_connection = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self._idle_timer is not None:
            self._idle_timer.cancel()

    async def async_set_persistent_connection(self, enabled: bool) -> None:
        """Enable or disable persistent BLE connection."""
        self._persistent_connection = enabled
        if enabled:
            # Cancel any pending idle disconnect
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            # Start keepalive loop
            self._start_keepalive()
        else:
            # Stop keepalive loop
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                self._keepalive_task = None
            # If connected, start idle timer so it disconnects normally
            if self._session.is_connected:
                self._reset_idle_timer()

    def _start_keepalive(self) -> None:
        """Start the keepalive loop that reconnects when BLE drops."""
        if self._keepalive_task and not self._keepalive_task.done():
            return
        # Use background task so it doesn't block HA startup
        self._keepalive_task = self._entry.async_create_background_task(
            self.hass, self._keepalive_loop(),
            f"tuya_ble_lock_keepalive_{self._mac}",
        )

    async def _keepalive_loop(self) -> None:
        """Periodically check connection and reconnect if needed."""
        backoff = 60
        # Stagger startup: wait a random 10-60s before first attempt
        await asyncio.sleep(random.uniform(10, 60))
        _LOGGER.debug("Persistent connection keepalive started for %s", self._mac)
        try:
            while self._persistent_connection and not self._stopping:
                if not self._session.is_connected:
                    _LOGGER.debug("Persistent connection: reconnecting %s...", self._mac)
                    async with self._op_lock:
                        try:
                            await self._async_ensure_connected()
                            await self._fetch_status()
                            self._start_listener()
                            backoff = 60  # reset on success
                        except Exception as exc:
                            _LOGGER.debug("Persistent reconnect failed for %s: %s", self._mac, exc)
                            backoff = min(backoff * 2, 600)  # max 10min
                # Add jitter to prevent thundering herd with multiple devices
                jitter = random.uniform(0.8, 1.2)
                await asyncio.sleep(backoff * jitter)
        except asyncio.CancelledError:
            pass
        _LOGGER.debug("Persistent connection keepalive stopped for %s", self._mac)

    async def _fetch_status(self) -> None:
        """Collect DP reports from the lock. Call while connected.

        On btScyChannel / protocol-5.0 firmwares (K3 BLE PRO 2) the
        CMD_DEVICE_STATUS (0x0003) response does include DPs — notably
        DP8 battery. On older firmwares (SYD8811, H8 Pro) it returns 0
        DPs and a trigger DP write is required instead. Try both.
        """
        try:
            await self._session.async_query_status()
        except Exception as exc:
            _LOGGER.debug("CMD_DEVICE_STATUS failed: %s", exc)

        battery_cfg = self._profile.get("entities", {}).get("battery_sensor")
        if battery_cfg:
            trigger_dp = battery_cfg.get("trigger_dp")
            trigger_hex = battery_cfg.get("trigger_payload")
            try:
                if trigger_dp and trigger_hex and self.state.get("battery_percent") is None:
                    trigger_payload = bytes.fromhex(trigger_hex)
                    await self._session.async_send_dp_raw(trigger_dp, trigger_payload)
                extra = await self._session._collect(timeout=2.0)
                _LOGGER.debug("Status collect: %d extra frames", len(extra))
                self._session._dispatch_dp_reports(extra)
            except Exception as exc:
                _LOGGER.warning("Status fetch failed: %s", exc)

    async def async_one_shot_status(self) -> None:
        """Single-attempt status fetch at startup. No retries."""
        async with self._op_lock:
            try:
                if not await self._session.async_connect_single_attempt():
                    _LOGGER.debug("One-shot status: lock not responding, skipping")
                    return
                await self._fetch_status()
                self._reset_idle_timer()
            except Exception as exc:
                _LOGGER.debug("One-shot status failed: %s", exc)
                await self._session.async_disconnect()

    async def _async_update_data(self) -> dict[str, Any]:
        """Connect to the lock and refresh all status DPs."""
        # Skip if we recently failed — don't spam BLE on every 12h poll
        since_fail = time.monotonic() - self._last_connect_failure
        if not self._session.is_connected and since_fail < CONNECT_COOLDOWN_SECONDS:
            _LOGGER.debug(
                "Poll: skipping %s, last connect failed %ds ago (cooldown %ds)",
                self._mac, int(since_fail), CONNECT_COOLDOWN_SECONDS,
            )
            return self.state
        async with self._op_lock:
            try:
                await self._async_ensure_connected()
                await self._fetch_status()
                self._reset_idle_timer()
            except UpdateFailed:
                _LOGGER.debug("Poll: BLE connect failed for %s, returning stale state", self._mac)
            except Exception as exc:
                _LOGGER.debug("Poll error for %s: %s", self._mac, exc)
        return self.state

    async def _async_ensure_connected(self) -> None:
        if not self._session.is_connected:
            if not await self._session.async_connect():
                self._last_connect_failure = time.monotonic()
                raise UpdateFailed("BLE connection to lock failed")

    def _build_unlock_payload(self, action_unlock: bool) -> bytes:
        """Build unlock/lock DP RAW payload (19 bytes).

        Format confirmed by sniff of Tuya app on K3 BLE PRO 2:
          [ff ff]       member_id (0xFFFF = admin)
          [00 01]       version
          [8B ASCII]    check code (from cloud DP71, per-device)
          [01/00]       action: 01=unlock, 00=lock
          [4B BE]       Unix timestamp
          [00 01]       trailer (observed in app sniff)

        Note: the DP report echoes back with member/version swapped
        ([00 01][ff ff]) and trailer 00 00 — that's the report format,
        not the write format.
        """
        code_str = self._device_data.get("check_code") or ""
        code = (code_str.encode("ascii") + b"\x00" * 8)[:8] if code_str else (
            DEFAULT_CHECK_CODE + b"\x00" * 8
        )[:8]
        ts = int(time.time())
        payload = struct.pack(">HH", 0xFFFF, 1)
        payload += code
        payload += bytes([0x01 if action_unlock else 0x00])
        payload += struct.pack(">I", ts)
        payload += b"\x00\x01"
        return payload

    def _get_unlock_dp(self) -> int:
        """Get the unlock DP ID from profile."""
        lock_cfg = self._profile.get("entities", {}).get("lock", {})
        return lock_cfg.get("unlock_dp", 71)

    async def async_lock(self) -> None:
        async with self._op_lock:
            await self._async_ensure_connected()
            unlock_dp = self._get_unlock_dp()
            payload = self._build_unlock_payload(action_unlock=False)
            _LOGGER.debug("Sending lock command (DP %d RAW, %d bytes): %s", unlock_dp, len(payload), payload.hex())
            try:
                await self._session.async_send_dp_fire_and_forget(unlock_dp, 0, payload)
            except Exception as exc:
                _LOGGER.warning("Lock command failed, reconnecting: %s", exc)
                self._session.is_connected = False
                await self._async_ensure_connected()
                payload = self._build_unlock_payload(action_unlock=False)
                await self._session.async_send_dp_fire_and_forget(unlock_dp, 0, payload)
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_unlock(self) -> None:
        async with self._op_lock:
            await self._async_ensure_connected()
            unlock_dp = self._get_unlock_dp()
            payload = self._build_unlock_payload(action_unlock=True)
            _LOGGER.debug("Sending unlock command (DP %d RAW, %d bytes): %s", unlock_dp, len(payload), payload.hex())
            try:
                await self._session.async_send_dp_fire_and_forget(unlock_dp, 0, payload)
            except Exception as exc:
                _LOGGER.warning("Unlock command failed, reconnecting: %s", exc)
                self._session.is_connected = False
                await self._async_ensure_connected()
                payload = self._build_unlock_payload(action_unlock=True)
                await self._session.async_send_dp_fire_and_forget(unlock_dp, 0, payload)
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_double_lock(self, enabled: bool) -> None:
        dl_cfg = self._profile.get("entities", {}).get("double_lock_switch")
        if not dl_cfg:
            _LOGGER.warning("Double lock not supported by this device profile")
            return
        dp = dl_cfg["dp"]
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp_bool(dp, enabled)
            self.state["double_lock"] = enabled
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_volume(self, volume: int) -> None:
        vol_cfg = self._profile.get("entities", {}).get("volume_select")
        if not vol_cfg:
            _LOGGER.warning("Volume control not supported by this device profile")
            return
        dp = vol_cfg["dp"]
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp(dp, 4, bytes([volume]))  # type=4 (ENUM)
            self.state["volume"] = volume
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_passage_mode(self, passage_on: bool) -> None:
        """Toggle passage mode. Inverted from DP 33 (auto_lock).

        passage_on=True  → auto_lock=False → lock stays open
        passage_on=False → auto_lock=True  → lock auto-locks normally
        """
        pm_cfg = self._profile.get("entities", {}).get("passage_mode_switch")
        if not pm_cfg:
            _LOGGER.warning("Passage mode not supported by this device profile")
            return
        dp = pm_cfg["dp"]
        auto_lock_val = not passage_on
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp_bool(dp, auto_lock_val)
            self.state["auto_lock"] = auto_lock_val
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_enum_dp(self, dp: int, value: int, state_key: str) -> None:
        """Send an enum DP value (type=4) and update state."""
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp(dp, 4, bytes([value]))
            self.state[state_key] = value
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_auto_lock_time(self, seconds: int) -> None:
        alt_cfg = self._profile.get("entities", {}).get("auto_lock_time_number")
        if not alt_cfg:
            _LOGGER.warning("Auto-lock time not supported by this device profile")
            return
        dp = alt_cfg["dp"]
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp(dp, 2, struct.pack(">I", seconds))  # type=2 (VALUE)
            self.state["auto_lock_time"] = seconds
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()
