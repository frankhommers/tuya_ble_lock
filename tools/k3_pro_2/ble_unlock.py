#!/usr/bin/env python3
"""Standalone BLE unlock for Tuya K3 BLE PRO 2 (btScyChannel, sec_flag 14/15).

Reverse-engineered protocol flow used to validate the integration:
  DEVICE_INFO (sec=14) → PAIR (sec=15, 76-byte payload) → TIME_V1 reply →
  DP_QUERY → DP71 unlock ([ffff][0001][code][01][ts][0001]).

Provide credentials via environment variables or --args. Fetch them first
with fetch_device_info.py.

Environment:
  LOCK_ADDR   BLE address (MAC on Linux/Android, CoreBluetooth UUID on macOS)
  LOCAL_KEY   16-char localKey from Tuya cloud
  SEC_KEY     16-char secKey from Tuya cloud
  DEVICE_UUID Tuya device UUID (e.g. "uuidc064f275f947")
  DEVICE_ID   Tuya devId
  CHECK_CODE  8-digit verification code (cloud DP71, per-device)

Usage:
  export LOCK_ADDR=... LOCAL_KEY=... SEC_KEY=... DEVICE_UUID=... \\
         DEVICE_ID=... CHECK_CODE=...
  python3 ble_unlock.py            # unlock
  python3 ble_unlock.py --lock     # lock
"""

import argparse
import asyncio
import hashlib
import os
import struct
import sys
import time

from bleak import BleakClient
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

WRITE_UUID = "00000001-0000-1001-8001-00805f9b07d0"
NOTIFY_UUID = "00000002-0000-1001-8001-00805f9b07d0"

DP_NAMES = {
    8: "battery%", 19: "ble_unlock", 31: "volume", 33: "auto_lock",
    36: "auto_lock_time", 40: "door_status", 46: "manual_lock",
    47: "motor_state", 70: "check_code_cfg", 71: "ble_unlock_verify",
}

buf: list[bytes] = []
SN = 1


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    sys.stdout.flush()


def _varint(v: int) -> bytes:
    r = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            b |= 0x80
        r.append(b)
        if not v:
            break
    return bytes(r)


def _crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack(">H", crc & 0xFFFF)


def build_frame(sn: int, ack_sn: int, cmd: int, data: bytes = b"") -> bytes:
    h = struct.pack(">IIHH", sn, ack_sn, cmd, len(data)) + data
    return h + _crc16(h)


def encrypt_frag(frame: bytes, key: bytes, sec_flag: int) -> list[bytes]:
    p = frame + b"\x00" * ((16 - len(frame) % 16) % 16)
    iv = os.urandom(16)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    payload = bytes([sec_flag]) + iv + enc.update(p) + enc.finalize()
    frags: list[bytes] = []
    off = idx = 0
    while off < len(payload):
        hdr = _varint(idx)
        if idx == 0:
            hdr += _varint(len(payload)) + b"\x20"
        chunk = payload[off : off + 20 - len(hdr)]
        frags.append(hdr + chunk)
        off += len(chunk)
        idx += 1
    return frags


def _decrypt(notifs: list[bytes], key: bytes) -> tuple[int, int, int, bytes]:
    idx0 = notifs[0]
    pos = 1
    total = 0
    shift = 0
    while pos < len(idx0):
        b = idx0[pos]
        total |= (b & 0x7F) << shift
        pos += 1
        shift += 7
        if not (b & 0x80):
            break
    pos += 1
    payload = idx0[pos:] + b"".join(n[1:] for n in notifs[1:])
    payload = payload[:total]
    iv = payload[1:17]
    ct = payload[17:]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = dec.update(ct) + dec.finalize()
    sn, ack, cmd, dlen = struct.unpack(">IIHH", plain[:12])
    return sn, ack, cmd, plain[12 : 12 + dlen]


def decode_messages(raw: list[bytes], key: bytes) -> list[dict]:
    msgs: list[dict] = []
    starts = [i for i, n in enumerate(raw) if n[0] == 0]
    for mi, s in enumerate(starts):
        e = starts[mi + 1] if mi + 1 < len(starts) else len(raw)
        try:
            sn, ack, cmd, data = _decrypt(raw[s:e], key)
            msgs.append({"sn": sn, "ack": ack, "cmd": cmd, "data": data})
        except Exception as ex:
            msgs.append({"error": str(ex)})
    return msgs


def parse_dps(data: bytes) -> list[tuple[int, int, bytes]]:
    dps: list[tuple[int, int, bytes]] = []
    if len(data) <= 6:
        return dps
    pos = 6
    while pos + 5 <= len(data):
        did = struct.unpack(">H", data[pos : pos + 2])[0]
        dt = data[pos + 2]
        dl = struct.unpack(">H", data[pos + 3 : pos + 5])[0]
        dps.append((did, dt, data[pos + 5 : pos + 5 + dl]))
        pos += 5 + dl
    return dps


def _on_notify(_sender, data: bytearray) -> None:
    buf.append(bytes(data))


async def _send(c: BleakClient, frags: list[bytes]) -> None:
    for f in frags:
        await c.write_gatt_char(WRITE_UUID, f, response=False)
        await asyncio.sleep(0.015)


async def _wait_initial(timeout: float, min_frags: int) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        await asyncio.sleep(0.03)
        if len(buf) >= min_frags:
            await asyncio.sleep(0.1)
            return True
    return bool(buf)


async def _handle_time(c: BleakClient, m: dict, key: bytes) -> bool:
    global SN
    if m["cmd"] == 0x8011:
        ts_ms = str(int(time.time() * 1000)).encode()
        tz = struct.pack(">h", -int(time.timezone / 36))
        await _send(c, encrypt_frag(build_frame(SN, m["sn"], 0x8011, ts_ms + tz), key, 15))
        SN += 1
        return True
    if m["cmd"] == 0x8012:
        t = time.localtime()
        td = struct.pack(
            ">BBBBBBBh",
            t.tm_year % 100, t.tm_mon, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec,
            t.tm_wday, -int(time.timezone / 36),
        )
        await _send(c, encrypt_frag(build_frame(SN, m["sn"], 0x8012, td), key, 15))
        SN += 1
        return True
    return False


async def collect(c: BleakClient, key: bytes, timeout: float = 2.5, idle: float = 0.4) -> list[dict]:
    """Collect notifications, handle time requests, exit early after `idle` seconds idle."""
    all_raw: list[bytes] = []
    deadline = time.time() + timeout
    last_rx = 0.0
    while time.time() < deadline:
        await asyncio.sleep(0.05)
        if buf:
            all_raw.extend(buf)
            buf.clear()
            last_rx = time.time()
            for m in decode_messages(all_raw, key):
                if m.get("cmd") in (0x8011, 0x8012):
                    await _handle_time(c, m, key)
        elif last_rx and time.time() - last_rx >= idle:
            break
    return decode_messages(all_raw, key) if all_raw else []


async def unlock(
    address: str,
    local_key: str,
    sec_key: str,
    device_uuid: str,
    device_id: str,
    check_code: str,
    action: int = 0x01,
) -> bool:
    """Connect, pair, and send DP71 lock/unlock. Returns True on motor trigger."""
    global SN
    SN = 1

    key14 = hashlib.md5((local_key + sec_key).encode()).digest()

    log(f"Connecting to {address}...")
    c = BleakClient(address, timeout=10.0)
    await c.connect()
    log(f"Connected (MTU={c.mtu_size})")
    try:
        await c.start_notify(NOTIFY_UUID, _on_notify)

        buf.clear()
        await _send(c, encrypt_frag(build_frame(1, 0, 0x0000, struct.pack(">H", 20)), key14, 14))
        SN = 2
        if not await _wait_initial(2.5, 4):
            log("No device_info response")
            return False
        _, _, _, di_data = _decrypt(list(buf), key14)
        srand = di_data[6:12]
        log(f"device_info OK srand={srand.hex()} proto={di_data[2]}.{di_data[3]}")
        key15 = hashlib.md5((local_key + sec_key).encode() + srand).digest()

        # PAIR (btScyChannel format: uuid + login6 + virtual22 + local16 + sec16)
        pair = (
            device_uuid.encode()[:16]
            + local_key[:6].encode()
            + (device_id.encode() + b"\x00" * 22)[:22]
            + local_key.encode()[:16]
            + sec_key.encode()[:16]
        )
        buf.clear()
        await _send(c, encrypt_frag(build_frame(SN, 0, 0x0001, pair), key15, 15))
        SN += 1
        msgs = await collect(c, key15, timeout=2.5)
        if not any(
            m.get("cmd") == 0x0001 and m.get("data", b"")[:1] in (b"\x00", b"\x02")
            for m in msgs
        ):
            log("PAIR failed")
            return False
        log("PAIR OK")

        # DP_QUERY — logs current DP state (battery, etc.)
        buf.clear()
        await _send(c, encrypt_frag(build_frame(SN, 0, 0x0003, b""), key15, 15))
        SN += 1
        for m in await collect(c, key15, timeout=2.0):
            if m.get("cmd") == 0x8006:
                for did, _dt, dv in parse_dps(m["data"]):
                    name = DP_NAMES.get(did, f"dp{did}")
                    val = int.from_bytes(dv, "big") if dv else 0
                    log(f"  {name} = {val} (raw={dv.hex()})")

        # DP71 lock/unlock
        payload = (
            struct.pack(">HH", 0xFFFF, 1)
            + check_code.encode("ascii")[:8].ljust(8, b"\x00")
            + bytes([action])
            + struct.pack(">I", int(time.time()))
            + b"\x00\x01"
        )
        v4 = b"\x00\x00\x00\x00\x00" + struct.pack(">BBH", 71, 0, len(payload)) + payload
        buf.clear()
        await _send(c, encrypt_frag(build_frame(SN, 0, 0x0027, v4), key15, 15))
        SN += 1
        word = "UNLOCK" if action == 0x01 else "LOCK"
        log(f">>> DP71 {word} sent")

        motor_triggered = False
        for m in await collect(c, key15, timeout=4.0):
            if m.get("cmd") == 0x8006:
                for did, _dt, dv in parse_dps(m["data"]):
                    name = DP_NAMES.get(did, f"dp{did}")
                    val = int.from_bytes(dv, "big") if dv else 0
                    log(f"  {name} = {val} (raw={dv.hex()})")
                    if did == 47 and val == 1:
                        motor_triggered = True
        return motor_triggered
    finally:
        if c.is_connected:
            await c.disconnect()


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Missing env var {name}", file=sys.stderr)
        sys.exit(2)
    return v


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", action="store_true", help="Send lock (action=0) instead of unlock (action=1)")
    args = ap.parse_args()

    ok = asyncio.run(
        unlock(
            address=_require_env("LOCK_ADDR"),
            local_key=_require_env("LOCAL_KEY"),
            sec_key=_require_env("SEC_KEY"),
            device_uuid=_require_env("DEVICE_UUID"),
            device_id=_require_env("DEVICE_ID"),
            check_code=_require_env("CHECK_CODE"),
            action=0x00 if args.lock else 0x01,
        )
    )
    log("Motor triggered!" if ok else "No motor state change reported.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
