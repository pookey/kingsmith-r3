"""Passive BLE scan: list nearby devices and flag likely WalkingPads.

Usage: uv run tools/scan.py [--timeout 10] [--all]
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from bleak import BleakScanner

KNOWN_SERVICES = {
    "0000fe00-0000-1000-8000-00805f9b34fb": "WiLink (legacy)",
    "00001826-0000-1000-8000-00805f9b34fb": "FTMS",
    "24e2521c-f63b-48ed-85be-c5330a00fdf7": "KS Supplement",
    "00001234-0000-1000-8000-00805f9b34fb": "props/cipher",
    "00011234-0000-1000-8000-00805f9b34fb": "props/cipher",
    "00021234-0000-1000-8000-00805f9b34fb": "props/cipher",
}
NAME_HINTS = ("ks-", "kingsmith", "walkingpad", "zp-zeal")


def is_candidate(name, uuids):
    n = (name or "").lower()
    return n.startswith(NAME_HINTS) or any(u.lower() in KNOWN_SERVICES for u in uuids)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--all", action="store_true", help="print every device, not just candidates")
    args = ap.parse_args()

    print(f"Scanning for {args.timeout}s ...")
    found = await BleakScanner.discover(timeout=args.timeout, return_adv=True)

    rows = []
    for addr, (dev, adv) in found.items():
        name = adv.local_name or dev.name
        cand = is_candidate(name, adv.service_uuids)
        row = {
            "address": addr,
            "name": name,
            "rssi": adv.rssi,
            "service_uuids": adv.service_uuids,
            "protocols": sorted({KNOWN_SERVICES[u.lower()] for u in adv.service_uuids if u.lower() in KNOWN_SERVICES}),
            "manufacturer_data": {str(k): v.hex() for k, v in adv.manufacturer_data.items()},
            "service_data": {k: v.hex() for k, v in adv.service_data.items()},
            "candidate": cand,
        }
        rows.append(row)

    rows.sort(key=lambda r: (not r["candidate"], -(r["rssi"] or -999)))
    for r in rows:
        if not (r["candidate"] or args.all):
            continue
        flag = "***" if r["candidate"] else "   "
        print(f"{flag} {r['rssi']:>4} dBm  {r['name']!s:<28} {r['address']}")
        for k in ("protocols", "service_uuids", "manufacturer_data", "service_data"):
            if r[k]:
                print(f"        {k}: {r[k]}")

    n_cand = sum(r["candidate"] for r in rows)
    print(f"\n{len(rows)} devices seen, {n_cand} candidate(s).")

    out = Path("captures") / f"scan-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f"Saved {out}")


if __name__ == "__main__":
    asyncio.run(main())
