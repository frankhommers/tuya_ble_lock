#!/usr/bin/env python3
"""Decode btsnoop_hci.log traffic for a Tuya btScyChannel lock (sec_flags 14/15).

Unlike the generic decode_btsnoop.py in the parent directory, this decoder
supports the "new security" framing used by K3 BLE PRO 2 and similar
protocol-5.0 devices where the keys are MD5(local_key + sec_key [+ srand])
rather than MD5(login_key[:6]).

Capture the HCI log on Android via Developer Options → "Bluetooth HCI snoop
log", adb pull /data/misc/bluetooth/logs/btsnoop_hci.log (path varies).

Usage:
  python3 decode_sniff.py capture.btsnoop \\
    --local-key '<c.ppneW0L&mn0wR' \\
    --sec-key   'HGQjhdgsh|MhUWZR'

The lock-side ACL connection handle is auto-detected from the first
Enhanced Connection Complete event matching the target MAC (if given).
"""

import argparse
import hashlib
import struct
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DP_NAMES = {
    8: "battery", 19: "ble_unlock", 31: "volume", 33: "auto_lock",
    34: "2fa_mode", 40: "door", 46: "manual_lock", 47: "motor_state",
    62: "remote_phone_unlock", 70: "check_code_cfg",
    71: "ble_unlock_verify", 73: "remote_unlock_cfg",
}
CMD_NAMES = {
    0x0000: "DEVICE_INFO", 0x0001: "PAIR", 0x0003: "DP_QUERY",
    0x0027: "DP_WRITE_V4", 0x8006: "DP_REPORT_V4",
    0x8011: "TIME_V1", 0x8012: "TIME_V2",
}


def _parse_mac(arg: str) -> bytes | None:
    if not arg:
        return None
    clean = arg.replace(":", "").replace("-", "").upper()
    if len(clean) != 12:
        return None
    return bytes.fromhex(clean)


def _iter_hci(path: str):
    """Yield (ts64, pkt_bytes) for each HCI record in a btsnoop file."""
    with open(path, "rb") as f:
        f.read(16)  # file header
        while True:
            r = f.read(24)
            if len(r) < 24:
                return
            _orig, incl, _flags, _drop, ts64 = struct.unpack(">IIIIQ", r)
            pkt = f.read(incl)
            if pkt:
                yield ts64, pkt


def _find_lock_handle(path: str, target_mac_le: bytes | None) -> int | None:
    """Scan LE Enhanced Connection Complete events for the target MAC."""
    for _ts, pkt in _iter_hci(path):
        if len(pkt) < 4 or pkt[0] != 4 or pkt[1] != 0x3E:
            continue
        sub = pkt[3]
        if sub == 0x0A and len(pkt) >= 15:  # Enhanced Connection Complete
            status = pkt[4]
            handle = struct.unpack("<H", pkt[5:7])[0] & 0x0FFF
            peer = pkt[9:15]
            if status == 0 and (target_mac_le is None or peer == target_mac_le):
                return handle
        elif sub == 0x01 and len(pkt) >= 14:  # Connection Complete (legacy)
            status = pkt[4]
            handle = struct.unpack("<H", pkt[5:7])[0] & 0x0FFF
            peer = pkt[8:14]
            if status == 0 and (target_mac_le is None or peer == target_mac_le):
                return handle
    return None


def iter_att(path: str, handle: int):
    """Yield (ts, direction, att_value) for ATT writes/notifications on handle."""
    for ts, pkt in _iter_hci(path):
        if len(pkt) < 9 or pkt[0] != 2:
            continue
        hf = struct.unpack("<H", pkt[1:3])[0]
        if (hf & 0x0FFF) != handle:
            continue
        acl_len = struct.unpack("<H", pkt[3:5])[0]
        l2 = pkt[5:5 + acl_len]
        if len(l2) < 4:
            continue
        l2_len, cid = struct.unpack("<HH", l2[:4])
        if cid != 0x0004:
            continue
        att = l2[4:4 + l2_len]
        if len(att) < 3:
            continue
        op = att[0]
        val = att[3:]
        if op in (0x12, 0x52):
            yield ts, "APP->LOCK", val
        elif op in (0x1B, 0x1D):
            yield ts, "LOCK->APP", val


def _varint(b: bytes, i: int = 0) -> tuple[int, int]:
    v = 0
    sh = 0
    while i < len(b):
        c = b[i]
        i += 1
        v |= (c & 0x7F) << sh
        if not (c & 0x80):
            return v, i
        sh += 7
    return v, i


def reassemble(events):
    """Reassemble fragmented Tuya BLE messages per direction."""
    bufs: dict[str, list] = {"APP->LOCK": [], "LOCK->APP": []}
    start_ts: dict[str, int] = {"APP->LOCK": 0, "LOCK->APP": 0}
    for ts, d, val in events:
        if not val:
            continue
        if not bufs[d]:
            idx, i = _varint(val, 0)
            if idx != 0:
                continue
            total_len, i = _varint(val, i)
            i += 1  # version byte
            bufs[d] = [val[i:], total_len]
            start_ts[d] = ts
            if sum(len(x) for x in bufs[d] if isinstance(x, bytes)) >= total_len:
                payload = b"".join(x for x in bufs[d] if isinstance(x, bytes))[:total_len]
                yield start_ts[d], d, payload
                bufs[d] = []
        else:
            _idx, i = _varint(val, 0)
            bufs[d].insert(-1, val[i:])
            total_len = bufs[d][-1]
            chunks = [x for x in bufs[d] if isinstance(x, bytes)]
            if sum(len(c) for c in chunks) >= total_len:
                payload = b"".join(chunks)[:total_len]
                yield start_ts[d], d, payload
                bufs[d] = []


def decrypt(payload: bytes, key14: bytes, key15: bytes | None) -> tuple[bytes | None, int]:
    if len(payload) < 17:
        return None, 0
    sec = payload[0]
    iv = payload[1:17]
    ct = payload[17:]
    if sec == 14:
        key = key14
    elif sec == 15 and key15:
        key = key15
    else:
        return None, sec
    try:
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return d.update(ct) + d.finalize(), sec
    except Exception:
        return None, sec


def parse_frame(plain: bytes) -> dict | None:
    if len(plain) < 14:
        return None
    sn, ack, cmd, dlen = struct.unpack(">IIHH", plain[:12])
    return {"sn": sn, "ack": ack, "cmd": cmd, "data": plain[12:12 + dlen]}


def parse_write_dps(data: bytes) -> list[tuple[int, int, bytes]]:
    """Parse APP->LOCK DP write payload: [5B hdr][dp:1][type:1][len:2][val]..."""
    out: list[tuple[int, int, bytes]] = []
    pos = 5
    while pos + 4 <= len(data):
        dp = data[pos]
        dt = data[pos + 1]
        dl = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        out.append((dp, dt, data[pos + 4:pos + 4 + dl]))
        pos += 4 + dl
        if dl == 0:
            break
    return out


def parse_report_dps(data: bytes) -> list[tuple[int, int, bytes]]:
    """Parse LOCK->APP DP report payload: [6B hdr][dp_id:2][type:1][len:2][val]..."""
    out: list[tuple[int, int, bytes]] = []
    pos = 6
    while pos + 5 <= len(data):
        did = struct.unpack(">H", data[pos:pos + 2])[0]
        dt = data[pos + 2]
        dl = struct.unpack(">H", data[pos + 3:pos + 5])[0]
        out.append((did, dt, data[pos + 5:pos + 5 + dl]))
        pos += 5 + dl
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="btsnoop_hci.log path")
    ap.add_argument("--local-key", required=True, help="16-char localKey")
    ap.add_argument("--sec-key", required=True, help="16-char secKey")
    ap.add_argument("--mac", default="", help="Lock MAC (optional; first conn picked if omitted)")
    args = ap.parse_args()

    mac_le = None
    if args.mac:
        mac_be = _parse_mac(args.mac)
        if not mac_be:
            print(f"Invalid MAC: {args.mac}", file=sys.stderr)
            sys.exit(2)
        mac_le = bytes(reversed(mac_be))

    handle = _find_lock_handle(args.file, mac_le)
    if handle is None:
        print("Could not locate lock connection handle in capture", file=sys.stderr)
        sys.exit(1)
    print(f"# Using ACL handle {handle}")

    lk = args.local_key.encode("utf-8")
    sk = args.sec_key.encode("utf-8")
    key14 = hashlib.md5(lk + sk).digest()

    events = list(iter_att(args.file, handle))
    print(f"# ATT events on lock: {len(events)}")

    key15: bytes | None = None
    for ts, direction, payload in reassemble(events):
        plain, sec = decrypt(payload, key14, key15)
        if plain is None:
            print(f"[{direction}] sec={sec} enc_len={len(payload)} (no key yet)")
            continue
        fr = parse_frame(plain)
        if not fr:
            print(f"[{direction}] sec={sec} plain={plain[:20].hex()} (malformed)")
            continue
        cmd = fr["cmd"]
        data = fr["data"]
        name = CMD_NAMES.get(cmd, f"0x{cmd:04x}")
        print(f"[{direction}] sec={sec} sn={fr['sn']} ack={fr['ack']} {name} dlen={len(data)}")

        if cmd == 0x0000 and direction == "LOCK->APP" and len(data) >= 12:
            srand = data[6:12]
            key15 = hashlib.md5(lk + sk + srand).digest()
            print(f"    srand={srand.hex()} proto={data[2]}.{data[3]} bound={data[5]}")
        elif cmd == 0x0027:
            for did, dt, dv in parse_write_dps(data):
                label = DP_NAMES.get(did, "?")
                ascii_repr = dv.decode("ascii", errors="replace")
                print(f"    DP{did} ({label}) type={dt} val={dv.hex()} ascii={ascii_repr!r}")
        elif cmd == 0x8006:
            for did, dt, dv in parse_report_dps(data):
                label = DP_NAMES.get(did, "?")
                ascii_repr = dv.decode("ascii", errors="replace")
                print(f"    DP{did} ({label}) type={dt} val={dv.hex()} ascii={ascii_repr!r}")
        elif cmd == 0x0001 and direction == "APP->LOCK" and len(data) >= 16:
            print(f"    uuid={data[:16]!r}  full_pair_len={len(data)}")
        elif data:
            print(f"    raw={data.hex()}")


if __name__ == "__main__":
    main()
