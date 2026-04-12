<p align="center">
  <img src="logo.png" width="128" height="128" alt="Tuya BLE Lock icon">
</p>

<h1 align="center">Tuya BLE Lock</h1>

<p align="center">
  Local Bluetooth smart lock integration for Home Assistant.<br>
  Zero cloud dependency after initial setup.
</p>

---

A [Home Assistant](https://www.home-assistant.io/) custom integration for controlling Tuya-based BLE smart locks **entirely over local Bluetooth**. Cloud credentials are used once during setup to fetch encryption keys — after that, all lock/unlock operations, credential management, and status updates happen directly over BLE.

Forked from [tkhadimullin/tuya_ble_lock](https://github.com/tkhadimullin/tuya_ble_lock) with enhanced config flow and additional device profiles.

## Features

- Lock/unlock via Home Assistant UI or automations
- Battery monitoring
- Credential management (PINs, fingerprints, NFC cards)
- Temporary passwords with time limits
- Lock settings (volume, auto-lock, privacy lock)
- State persistence across HA restarts
- **Persistent BLE connection mode** for instant response with BLE proxies
- **Country dropdown** with auto-detected region and country code

## Supported Hardware

| Device | Product ID | Chip | Protocol | Notes |
|--------|-----------|------|----------|-------|
| [Smart Lock 3](https://manuals.plus/asin/B0CD1CHYK8) | `qqmu5mit` | SYD8811 | V4 | DP 520 battery |
| [H8 Pro](https://manuals.plus/asin/B0FDB2NSP3) | `wwbdbt3h` | — | V3 | Passage mode, auto-lock timer |
| K3 BLE PRO 2 Keybox | `ba2qk177` | — | V4 | Smart keybox |

Other Tuya BLE locks in the `jtmspro` or `jtmsbh` categories will likely work with the default profile. See [Adding New Devices](#adding-new-devices) to create a profile for your lock.

## Prerequisites

1. A **Bluetooth adapter** on your Home Assistant host (built-in, USB, or ESPHome BLE proxy)
2. Home Assistant 2024.1 or later
3. A **Tuya Smart** or **Smart Life** app account with your locks paired to it

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** > ⋮ > **Custom repositories**
3. Add `https://github.com/frankhommers/tuya_ble_lock` as an **Integration**
4. Search for "Tuya BLE Lock" and install
5. Restart Home Assistant

### Manual

1. Download this repository
2. Copy the `custom_components/tuya_ble_lock` folder to your HA `config/custom_components/` directory
3. Restart Home Assistant

## Setup

### First-Time Setup

1. Go to **Settings > Devices & Services > Add Integration > Tuya BLE Lock**
2. **Select your country** from the dropdown — region and country code are auto-detected
3. Enter your **Tuya Smart / Smart Life** app email and password
4. The integration creates a "Tuya BLE Locks" hub
5. Any locks already discovered via Bluetooth are added automatically

For "Other (manual)" you can enter a custom country code and select the region from a dropdown.

### Adding More Locks

Once the hub is set up, **new locks are added automatically**:

1. Pair the lock to your Tuya Smart / Smart Life app (if not already)
2. Ensure the lock is powered on and within Bluetooth range
3. Home Assistant discovers the lock via BLE advertisement
4. The integration fetches the encryption keys from the cloud using your saved credentials
5. The lock appears as a new device — no user interaction needed

### Why Cloud Credentials Are Needed

Tuya BLE locks use a unique encryption key (called an "auth key") assigned during manufacturing. This key is stored on Tuya's cloud servers and is required to establish a secure BLE session with the lock. There is no way to extract it from the lock itself.

During setup, the integration logs into the Tuya cloud API to retrieve this key:

- **Your credentials are stored locally** in the hub config entry
- **The lock is not re-associated** — the integration simply reads the device's auth key
- **No cloud connection for ongoing operations** — all lock/unlock commands happen entirely over local Bluetooth

### Coexistence with the Tuya App

This integration does **not** remove your lock from the Tuya app. You can continue using both.

However, BLE locks only support **one active connection at a time**:

- If the **Tuya app** is connected, HA will not be able to connect until the app disconnects
- If **HA** is holding a connection (within 60s of last operation, or with persistent connection enabled), the Tuya app cannot connect

In practice this is rarely an issue — the integration disconnects after 60 seconds of inactivity, and the app only connects briefly. PINs, fingerprints, and cards always work regardless of which controller is connected.

## Entities

Each lock creates the following entities:

| Entity | Type | Description |
|--------|------|-------------|
| Lock | `lock` | Main lock/unlock control |
| Battery | `sensor` | Battery percentage (0-100%) |
| Battery state | `sensor` | Qualitative level: high, medium, low, exhausted |
| Privacy lock | `switch` | Electronic double-lock (DP 79) |
| Passage mode | `switch` | Keep lock unlocked until manually locked (model-dependent) |
| Persistent connection | `switch` | Keep BLE connection alive for instant response |
| Volume | `select` | Keypad sound level |
| Auto-lock delay | `number` | Seconds before auto-lock (model-dependent) |
| Refresh status | `button` | Force a BLE status refresh |

Diagnostic entities (UUID, login key, virtual ID, auth key) are hidden by default.

## Lock Behaviour

### Auto-Lock

Tuya BLE locks automatically re-lock after unlocking. The integration tracks state through motor feedback (DP 47) and passage mode sync (DP 33).

### Passage Mode

Keeps the lock **permanently unlocked** until manually turned off. Useful for business hours, parties, or maintenance. Only available on models that support DP 33 (e.g. H8 Pro).

### Privacy Lock (Double Lock)

Adds an extra electronic lock engagement — regular credentials won't work, only admin credentials may bypass it. Controlled via DP 79.

### Persistent Connection

By default, the integration disconnects after 60 seconds of inactivity. Enable **Persistent connection** to keep the BLE link alive:

- Lock/unlock in ~1 second (no reconnect overhead)
- Real-time DP push reports
- Auto-reconnect with exponential backoff if connection drops

Trade-offs: increased battery drain, occupies a BLE slot on the proxy, and the Tuya app can't connect simultaneously.

## Services

All services are available under the `tuya_ble_lock` domain in **Developer Tools > Services**.

### add_pin

Enroll a PIN code (6-10 digits).

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | Yes | Lock device(s) |
| `pin_code` | Yes | PIN digits |
| `person` | No | HA person to associate |
| `admin` | No | Admin credential (default: false) |

Supports multiple devices for enrolling the same PIN across all locks at once.

### add_fingerprint

Start fingerprint enrollment. User must place their finger on the sensor multiple times (4-6 touches). 60-second timeout.

### add_card

Start NFC/RFID card enrollment. Tap the card within 30 seconds.

### create_temp_password

Create a time-limited temporary password.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | Yes | Lock device |
| `name` | Yes | Password label |
| `pin_code` | Yes | PIN digits |
| `effective_time` | Yes | Start time (datetime) |
| `expiry_time` | Yes | End time (datetime) |

### delete_credential

Delete credentials by person, type, or specific ID.

```yaml
service: tuya_ble_lock.delete_credential
data:
  device_id: <lock_device_id>
  person: person.guest
  cred_type: pin  # optional: pin, fingerprint, or card
```

### list_credentials

List all enrolled credentials for a lock. Use with "Return response" enabled.

## Automation Examples

### Guest Access

```yaml
automation:
  - alias: "Add guest PIN on arrival"
    trigger:
      - platform: state
        entity_id: input_boolean.guest_mode
        to: "on"
    action:
      - service: tuya_ble_lock.add_pin
        data:
          device_id: <lock_device_id>
          pin_code: "987654"
```

### Temporary Cleaner Access

```yaml
automation:
  - alias: "Monday cleaner password"
    trigger:
      - platform: time
        at: "06:00:00"
    condition:
      - condition: time
        weekday: [mon]
    action:
      - service: tuya_ble_lock.create_temp_password
        data:
          device_id: <lock_device_id>
          name: "Cleaner {{ now().strftime('%b %d') }}"
          pin_code: "{{ range(1000, 9999) | random }}"
          effective_time: "{{ now().replace(hour=8, minute=0).isoformat() }}"
          expiry_time: "{{ now().replace(hour=18, minute=0).isoformat() }}"
```

## Adding New Devices

The integration uses JSON device profiles. To add a new lock:

1. Copy `_default.json` as a starting point
2. Rename to your lock's `product_id`, e.g. `abc123de.json`
3. Fill in the profile fields:

```json
{
  "product_id": "abc123de",
  "name": "My Lock Model",
  "model": "Model Name",
  "category": "jtmspro",
  "entities": {
    "lock": { "unlock_dp": 71 },
    "battery_sensor": { "dp": [8] },
    "volume_select": {
      "dp": 31, "dp_type": "enum",
      "options": ["mute", "normal"]
    }
  },
  "services": {
    "add_pin": { "dp": 1 },
    "delete_credential": { "dp": 2 }
  },
  "state_map": {
    "8":  { "key": "battery_percent", "parse": "int" },
    "47": { "key": "motor_state", "parse": "bool" },
    "31": { "key": "volume", "parse": "raw_byte" }
  }
}
```

### Profile Reference

| Field | Description |
|-------|-------------|
| `entities.lock.unlock_dp` | DP for lock/unlock (usually 71) |
| `entities.battery_sensor.dp` | DP(s) for battery percentage |
| `entities.battery_sensor.trigger_dp` + `trigger_payload` | Optional: trigger battery report |
| `entities.volume_select` | Volume control (DP 31) |
| `entities.double_lock_switch` | Privacy lock (DP 79) |
| `entities.passage_mode_switch` | Passage mode (DP 33) |
| `entities.auto_lock_time_number` | Auto-lock delay (DP 36) |
| `state_map` | Maps incoming DPs to internal state keys |
| `protocol_version` | 3 or 4 (default: 4) |

### Parse Types

| Type | Description |
|------|-------------|
| `int` | Integer value |
| `bool` | Boolean |
| `raw_byte` | Single byte as integer |
| `battery_state_enum` | Maps to high/medium/low/exhausted |
| `ignore` | DP is received but discarded |

## Troubleshooting

### Lock not discovered
- Ensure the lock is **powered on** and within Bluetooth range (~5m)
- Wake the lock by touching the keypad or fingerprint sensor
- Check that your HA host has a working Bluetooth adapter

### Lock not auto-added
- The lock must be **paired to your Tuya app** first
- Check HA logs for "cloud_fetch_failed" messages

### Operations are slow
- First operation after idle may take 5-15 seconds (BLE reconnect)
- Enable **Persistent connection** to eliminate reconnect delays
- Close the Tuya app if it's holding the BLE connection

### Battery shows "Unknown"
- Battery is polled every 12 hours — press "Refresh status" for immediate read

## Credits

Based on [tkhadimullin/tuya_ble_lock](https://github.com/tkhadimullin/tuya_ble_lock) and the [python-tuya-ble](https://github.com/redphx/python-tuya-ble) protocol work by [redphx](https://github.com/redphx).

## License

MIT
