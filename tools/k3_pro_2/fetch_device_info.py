#!/usr/bin/env python3
"""Fetch BLE credentials + current check_code from Tuya cloud.

Use this to obtain the values needed for ble_unlock.py. All per-device keys
(localKey, secKey, DP71 check_code) are regenerated when the lock is
re-paired in the Tuya app, so re-run this after any re-pairing.

Environment:
  TUYA_USERNAME    Tuya account email
  TUYA_PASSWORD    Tuya account password
  TUYA_COUNTRY     country dialing code (default "31" = NL)
  TUYA_REGION      one of eu|us|cn|in (default "eu")
  TUYA_DEVICE_NAME optional substring to match device name; default "lock"

Usage:
  export TUYA_USERNAME=... TUYA_PASSWORD=...
  python3 fetch_device_info.py
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid as uuid_mod

import aiohttp

CERT_SIGN = (
    "93:21:9F:C2:73:E2:20:0F:4A:DE:E5:F7:19:1D:C6:56:"
    "BA:2A:2D:7B:2F:F5:D2:4C:D5:5C:4B:61:55:00:1E:40"
)
BMP_KEY = "f3hd7pet4p83kemjdf5wqsa5tavrv579"
CLIENT_ID = "3cxxt3au9x33ytvq3h9j"
APP_SECRET = "5gdtanjtf38vyxkqh87cjwfcqjhvjjqa"
HMAC_KEY = f"{CERT_SIGN}_{BMP_KEY}_{APP_SECRET}"
SIGN_PARAMS = {
    "a", "v", "lat", "lon", "et", "lang", "deviceId", "imei", "imsi",
    "appVersion", "ttid", "isH5", "h5Token", "os", "clientId", "postData",
    "time", "n4h5", "sid", "sp", "requestId",
}
REGIONS = {
    "us": "https://a1.tuyaus.com",
    "eu": "https://a1.tuyaeu.com",
    "cn": "https://a1.tuyacn.com",
    "in": "https://a1.tuyain.com",
}


def _post_hash(s: str) -> str:
    h = hashlib.md5(s.encode()).hexdigest()
    return h[8:16] + h[0:8] + h[24:32] + h[16:24]


def _sign(params: dict) -> str:
    parts = []
    for k in sorted(params):
        if k not in SIGN_PARAMS:
            continue
        v = str(params[k])
        if not v:
            continue
        if k == "postData":
            v = _post_hash(v)
        parts.append(f"{k}={v}")
    return hmac.new(HMAC_KEY.encode(), "||".join(parts).encode(), hashlib.sha256).hexdigest()


class TuyaMobileAPI:
    def __init__(self, session: aiohttp.ClientSession, region: str = "eu") -> None:
        self._s = session
        self._base = REGIONS.get(region, REGIONS["eu"])
        self._did = os.urandom(32).hex()
        self.sid = ""

    async def _call(self, action: str, version: str = "1.0", post: dict | None = None,
                    country: str = "", extra: dict | None = None) -> dict:
        params = {
            "a": action, "v": version, "clientId": CLIENT_ID,
            "deviceId": self._did, "os": "Android", "lang": "en",
            "ttid": "tuyaSmart", "appVersion": "7.2.8", "sdkVersion": "3.29.5",
            "time": str(int(time.time())), "requestId": str(uuid_mod.uuid4()),
        }
        if self.sid:
            params["sid"] = self.sid
        if country:
            params["countryCode"] = country
        if post is not None:
            params["postData"] = json.dumps(post, separators=(",", ":"))
        if extra:
            params.update(extra)
        params["sign"] = _sign(params)
        async with self._s.get(self._base + "/api.json", params=params,
                               headers={"User-Agent": "TuyaSmart/7.2.8 (Android)"}) as r:
            return await r.json()

    async def login(self, country: str, email: str, password: str) -> dict:
        r = await self._call(
            "thing.m.user.email.password.login", "3.0",
            {
                "countryCode": country, "email": email,
                "passwd": hashlib.md5(password.encode()).hexdigest(),
                "options": '{"group": 1}', "token": "", "ifencrypt": 0,
            },
            country=country,
        )
        if r.get("success"):
            self.sid = r["result"].get("sid", "")
        return r

    async def homes(self) -> dict:
        return await self._call("tuya.m.location.list", "2.1", {})

    async def devices(self, gid: int | str) -> dict:
        return await self._call(
            "tuya.m.my.group.device.list", "2.0", {}, extra={"gid": str(gid)}
        )


def _parse_check_code(dp71_b64: str) -> str:
    """Extract 8-digit ASCII check code from base64-encoded DP71 value."""
    try:
        raw = base64.b64decode(dp71_b64)
    except Exception:
        return ""
    # Format: [ver(2)][member(2)][code(8)][...]
    return raw[4:12].decode("ascii", errors="ignore") if len(raw) >= 12 else ""


async def main() -> None:
    email = os.environ.get("TUYA_USERNAME", "").strip()
    password = os.environ.get("TUYA_PASSWORD", "").strip()
    country = os.environ.get("TUYA_COUNTRY", "31").strip()
    region = os.environ.get("TUYA_REGION", "eu").strip()
    name_filter = os.environ.get("TUYA_DEVICE_NAME", "lock").strip().lower()

    if not email or not password:
        print("Set TUYA_USERNAME and TUYA_PASSWORD", file=sys.stderr)
        sys.exit(2)

    async with aiohttp.ClientSession() as session:
        api = TuyaMobileAPI(session, region=region)
        r = await api.login(country, email, password)
        if not r.get("success"):
            print(f"Login failed: {r.get('errorMsg') or r}", file=sys.stderr)
            sys.exit(1)
        print(f"# Logged in as {email}")

        h = await api.homes()
        homes = h.get("result", {})
        if isinstance(homes, dict):
            homes = homes.get("result", [])

        found = 0
        for home in homes:
            gid = home.get("groupId") or home.get("gid")
            if not gid:
                continue
            d = await api.devices(gid)
            devs = d.get("result", {})
            if isinstance(devs, dict):
                devs = devs.get("result", [])
            for dev in devs:
                if name_filter and name_filter not in (dev.get("name", "").lower()):
                    continue
                found += 1
                dpi = dev.get("dataPointInfo") or {}
                if isinstance(dpi, str):
                    try:
                        dpi = json.loads(dpi)
                    except Exception:
                        dpi = {}
                dp71 = (dpi.get("dps") or {}).get("71", "")
                check = _parse_check_code(dp71) if isinstance(dp71, str) else ""

                print()
                print(f"# Device: {dev.get('name')!r}  product={dev.get('productId')!r}")
                print(f"#   MAC (for Linux/Android): {dev.get('mac')}")
                print("# Paste these into your shell (fill LOCK_ADDR manually; on macOS")
                print("# it's the Core Bluetooth peripheral UUID, not the MAC).")
                print(f"export DEVICE_UUID={dev.get('uuid','')!r}")
                print(f"export DEVICE_ID={dev.get('devId','')!r}")
                print(f"export LOCAL_KEY={dev.get('localKey','')!r}")
                print(f"export SEC_KEY={dev.get('secKey','')!r}")
                print(f"export CHECK_CODE={check!r}")

        if not found:
            print(
                f"No device matched TUYA_DEVICE_NAME={name_filter!r}",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
