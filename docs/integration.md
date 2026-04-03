# Tuya BLE Lock - Integration Documentation

## Overview

Tuya BLE Lock is a Home Assistant custom integration for **local Bluetooth control** of Tuya-based smart door locks. After a one-time cloud-assisted setup, all daily operations (lock, unlock, credential management) happen entirely over BLE with **zero cloud dependency**.

Key features:
- Lock/unlock via Home Assistant UI or automations
- Battery monitoring
- Credential management (PINs, fingerprints, NFC cards)
- Temporary passwords with time limits
- Lock settings (volume, auto-lock, privacy lock)
- State persistence across HA restarts
- Persistent BLE connection mode for instant response (with BLE proxies)

## Prerequisites

1. A **Bluetooth adapter** on your Home Assistant host (built-in, USB, or ESPHome BLE proxy)
2. Home Assistant 2024.1 or later
3. A **Tuya Smart** or **Smart Life** app account with your locks paired to it

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** > three-dot menu > **Custom repositories**
3. Add `https://github.com/tkhadimullin/tuya_ble_lock` as an **Integration**
4. Search for "Tuya BLE Lock" and install
5. Restart Home Assistant

### Manual

1. Download this repository
2. Copy the `custom_components/tuya_ble_lock` folder to your HA `config/custom_components/` directory
3. Restart Home Assistant

## Setup

### Hub Architecture

The integration uses a **hub-based architecture**: you create a single integration entry for your Tuya cloud account, and all your BLE locks are grouped as devices under it.

### First-Time Setup

1. Go to **Settings > Devices & Services > Add Integration > Tuya BLE Lock**
2. Enter your Tuya Smart / Smart Life app email, password, country code, and cloud region
3. The integration creates a "Tuya BLE Locks" hub
4. Any locks already discovered via Bluetooth are added automatically

### Adding More Locks

Once the hub is set up, **new locks are added automatically**:

1. Pair the lock to your Tuya Smart / Smart Life app (if not already)
2. Ensure the lock is powered on and within Bluetooth range
3. Home Assistant discovers the lock via BLE advertisement
4. The integration fetches the encryption keys from the cloud using your saved credentials
5. The lock appears as a new device under the "Tuya BLE Locks" hub — no user interaction needed

### Why Cloud Credentials Are Needed

Tuya BLE locks use a unique encryption key (called an "auth key") assigned during manufacturing. This key is stored on Tuya's cloud servers and is required to establish a secure BLE session with the lock. There is no way to extract it from the lock itself.

During setup, the integration logs into the Tuya cloud API on your behalf to retrieve this key:

- **Your credentials are stored locally** in the hub config entry — they are never sent anywhere other than the Tuya cloud API during device setup.
- **The lock is not associated or re-associated** with the account — the integration simply reads the device's auth key
- **No cloud connection is ever made for ongoing operations** — all lock/unlock commands happen entirely over local Bluetooth. The saved credentials are only used when a new lock is discovered and needs setup.

### Coexistence with the Tuya App

Setting up this integration does **not** remove your lock from the Tuya Smart / Smart Life app. You can continue using the app to control the lock alongside Home Assistant.

However, BLE locks only support **one active connection at a time**. This means:

- If the **Tuya app** is connected to the lock (e.g., you have the lock's page open), Home Assistant will not be able to connect until the app disconnects
- If **Home Assistant** is holding a BLE connection (within 60 seconds of the last operation, or permanently if persistent connection is enabled), the Tuya app will not be able to connect until HA disconnects

In practice this is rarely an issue — the integration automatically disconnects after 60 seconds of inactivity, and the app only connects briefly when you interact with it. PINs, fingerprints, and cards always work regardless of which controller is connected, since they are processed locally by the lock.

### Upgrading from Per-Lock Entries

If you previously used a version of this integration that created separate config entries per lock, the upgrade is automatic. On first startup after updating:

1. All per-lock entries are merged into a single "Tuya BLE Locks" hub
2. Per-lock BLE credentials are moved to a device store
3. Credential records (PINs, fingerprints, etc.) are updated to reference locks by MAC address
4. The old per-lock entries are removed

No manual action is needed.

## Entities

Each lock creates the following entities:

| Entity | Type | Description |
|--------|------|-------------|
| Lock | `lock` | Main lock/unlock control. Tracks locked/unlocked state via motor feedback and passage mode sync. |
| Battery | `sensor` | Battery percentage (0-100%). Updated periodically via BLE. |
| Battery state | `sensor` | Qualitative battery level: high, medium, low, exhausted. |
| Privacy lock | `switch` | Electronic double-lock (DP 79). |
| Passage mode | `switch` | Keep lock unlocked until manually locked. Only on supported models (e.g., H8 Pro). |
| Persistent connection | `switch` | Keep BLE connection alive instead of disconnecting after 60s idle. Useful with ESPHome BLE proxies. |
| Volume | `select` | Keypad sound level. Options vary by model (mute/normal or mute/low/normal/high). |
| Auto-lock delay | `number` | Seconds before auto-lock engages. Only on models with passage mode. |
| Refresh status | `button` | Force a BLE status refresh. |
| UUID | `sensor` | Device UUID (diagnostic). |
| Login key | `sensor` | BLE login key (diagnostic). |
| Virtual ID | `sensor` | Device virtual ID (diagnostic). |
| Auth key | `sensor` | Device auth key (diagnostic). |

Volume, auto-lock delay, persistent connection, and refresh are **configuration entities** — they appear on the device page but not on the default dashboard.

Diagnostic sensors (UUID, login key, virtual ID, auth key) are hidden by default and only visible when "Show disabled entities" is enabled.

## Lock Behaviour

### Auto-Lock

Tuya BLE locks automatically re-lock after unlocking. Some models re-lock within a few seconds, while others have a configurable **Auto-lock delay** (in seconds) that you can adjust via the corresponding entity.

The integration tracks lock state through motor feedback (DP 47) and passage mode sync (DP 33). When the motor stops after an unlock, the state returns to "locked". This is reflected in Home Assistant automatically — no polling required.

### Passage Mode

Passage mode keeps the lock **permanently unlocked** until you manually turn it off. This is useful for:

- Keeping a door unlocked during business hours or a party
- Allowing free entry/exit without credentials
- Temporarily disabling the lock for maintenance or moving furniture

When passage mode is **on**:
- The lock does not auto-lock after being opened
- The bolt stays retracted
- Anyone can open the door without a PIN, fingerprint, or card
- The lock entity in HA shows "unlocked"

When passage mode is **off**:
- Normal auto-lock behaviour resumes
- The lock re-engages after the configured delay
- The lock entity in HA shows "locked"

Passage mode is controlled via the **Passage mode** switch entity. It is only available on models that support DP 33 (auto_lock), such as the H8 Pro. The Smart Lock 3 (SYD8811) does not support passage mode.

### Privacy Lock (Double Lock)

The privacy lock (also called double lock) adds an extra electronic lock engagement. When enabled:

- The lock cannot be opened with regular credentials (PINs, fingerprints, cards)
- Only admin credentials may be able to bypass it (model-dependent)
- Useful as a "do not disturb" / night lock mode

Controlled via the **Privacy lock** switch entity (DP 79).

## Persistent Connection

By default, the integration disconnects from the lock after 60 seconds of inactivity to conserve battery and free the BLE slot. If you have **ESPHome Bluetooth proxies** or dedicated BLE adapters with enough capacity, you can enable the **Persistent connection** switch on each lock to keep the BLE connection alive permanently.

When enabled:
- Lock/unlock commands execute in ~1 second (no reconnect overhead)
- DP push reports (motor state, physical unlock events) arrive in real-time
- If the connection drops (lock sleeps, proxy restarts, out of range), the integration automatically reconnects with exponential backoff (30s → 60s → 120s → max 5min)
- The setting persists across HA restarts

Trade-offs:
- **Battery**: The lock's BLE radio stays active, which may reduce battery life
- **BLE slots**: Each persistent connection occupies one BLE connection slot on the proxy/adapter (ESP32 supports ~3 concurrent connections by default)
- **App access**: The Tuya app cannot connect while HA holds the connection (see [Coexistence with the Tuya App](#coexistence-with-the-tuya-app))

## Services

All services are available under the `tuya_ble_lock` domain in **Developer Tools > Services**.

For detailed examples, automation recipes, and step-by-step enrollment walkthroughs, see the [Credential Management Guide](credential-management.md).

### add_pin

Enroll a PIN code on one or more locks.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | Yes | Lock device(s) — use MAC address or HA device ID |
| `pin_code` | Yes | PIN digits (6-10 digits) |
| `person` | No | HA person to associate with |
| `admin` | No | Whether this is an admin credential (default: false) |

### add_fingerprint

Start fingerprint enrollment. The user must place their finger on the sensor multiple times (typically 4-6 touches) when prompted by the lock.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | Yes | Lock device |
| `person` | No | HA person to associate with |
| `admin` | No | Admin credential (default: false) |

### add_card

Start NFC/RFID card enrollment. Tap the card on the lock's sensor when prompted.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | Yes | Lock device |
| `person` | No | HA person to associate with |
| `admin` | No | Admin credential (default: false) |

### delete_credential

Delete credentials from a lock. Specify either a person + type, or a specific credential ID.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | Yes | Lock device |
| `person` | No | Delete credentials belonging to this person |
| `cred_type` | No | Only delete this type: `pin`, `fingerprint`, or `card` |
| `credential_id` | No | Delete a specific credential by UUID (from `list_credentials`) |

### list_credentials

Returns all credentials stored for a lock, grouped by member and type.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | Yes | Lock device |

### create_temp_password

Create a time-limited temporary password on the lock.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | Yes | Lock device |
| `name` | Yes | Password name/label |
| `pin_code` | Yes | PIN digits |
| `effective_time` | Yes | Start time (datetime) |
| `expiry_time` | Yes | End time (datetime) |

## Device Profiles

The integration uses JSON device profiles to handle differences between lock models. Profiles are stored in `custom_components/tuya_ble_lock/device_profiles/`.

Each profile defines:
- Which entities to create and their DP mappings
- How to parse DP reports (`state_map`)
- Which services are available and their DP assignments
- Protocol version (V3 or V4)

The correct profile is auto-selected based on the `product_id` reported by the lock. If no matching profile exists, a default profile is used.

### Currently Supported Devices

| Device | Product ID | Profile |
|--------|-----------|---------|
| Smart Lock 3 (SYD8811) | `qqmu5mit` | `qqmu5mit.json` |
| H8 Pro | `wwbdbt3h` | `wwbdbt3h.json` |
| Generic Tuya BLE Lock | — | `_default.json` |

See the [Adding New Devices](../README.md#adding-new-devices) section in the README for how to create profiles for other locks.

## Troubleshooting

### Lock not discovered

- Ensure the lock is **powered on** and within Bluetooth range (~5m)
- Wake the lock by touching the keypad or fingerprint sensor
- Check that your HA host has a working Bluetooth adapter (`bluetoothctl show`)

### Lock not auto-added

- The lock must be **paired to your Tuya Smart / Smart Life app** before it can be auto-added
- Check HA logs for "cloud_fetch_failed" messages — this usually means the lock isn't on your Tuya account
- Ensure your cloud credentials in the hub entry are still valid

### Lock shows "Unavailable"

- BLE locks sleep aggressively to save battery. The integration reconnects on demand.
- Press the refresh button or trigger a lock/unlock to wake the connection
- Check HA logs for BLE connection errors

### Operations are slow

- First operation after idle may take 5-15 seconds (BLE reconnect + handshake)
- Subsequent operations within 60 seconds are fast (~1 second) due to idle-disconnect caching
- If the Tuya app is open on the lock's page, it may be holding the BLE connection — close the app and try again (see [Coexistence with the Tuya App](#coexistence-with-the-tuya-app))
- Enable **Persistent connection** to eliminate reconnect delays entirely

### Battery shows "Unknown"

- Battery is polled every 12 hours via BLE
- Press "Refresh status" to trigger an immediate battery read
- Some models only report battery state (high/medium/low) rather than exact percentage
