"""Read-only GATT dump: connect, enumerate services/characteristics/descriptors,
read every readable characteristic (except OTA), save to captures/.

Usage: uv run tools/gatt_dump.py [--name KS-AP-RQ3] [--address <uuid>]
"""

import argparse
import asyncio
import json
import struct
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

# Never touch these (firmware update channels).
SKIP_PREFIXES = ("32e2314c-",)

STRING_CHARS = {
    "00002a00": "Device Name",
    "00002a24": "Model Number",
    "00002a25": "Serial Number",
    "00002a26": "Firmware Revision",
    "00002a27": "Hardware Revision",
    "00002a28": "Software Revision",
    "00002a29": "Manufacturer Name",
}


def decode(uuid: str, data: bytes):
    short = uuid[:8]
    if short in STRING_CHARS:
        return data.decode("utf-8", "replace")
    if short == "00002acc" and len(data) >= 8:
        feat, target = struct.unpack_from("<II", data)
        return {"features": f"0x{feat:08x}", "target_settings": f"0x{target:08x}"}
    if short == "00002ad4" and len(data) >= 6:
        mn, mx, inc = struct.unpack_from("<HHH", data)
        return {"min_kmh": mn / 100, "max_kmh": mx / 100, "inc_kmh": inc / 100}
    if short == "00002ad5" and len(data) >= 6:
        mn, mx, inc = struct.unpack_from("<hhH", data)
        return {"min_pct": mn / 10, "max_pct": mx / 10, "inc_pct": inc / 10}
    if all(32 <= b < 127 for b in data) and data:
        return data.decode()
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="KS-AP-RQ3")
    ap.add_argument("--address")
    args = ap.parse_args()

    if args.address:
        target = args.address
    else:
        print(f"Looking for {args.name} ...")
        target = await BleakScanner.find_device_by_name(args.name, timeout=15)
        if not target:
            raise SystemExit("Device not found")
    print(f"Connecting to {target} ...")

    out = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "services": []}
    async with BleakClient(target, timeout=20) as client:
        print(f"Connected. MTU={client.mtu_size}")
        out["mtu"] = client.mtu_size
        for svc in client.services:
            s = {"uuid": svc.uuid, "handle": svc.handle, "description": svc.description, "characteristics": []}
            print(f"\n[Service] {svc.uuid}  {svc.description}")
            for ch in svc.characteristics:
                c = {
                    "uuid": ch.uuid,
                    "handle": ch.handle,
                    "description": ch.description,
                    "properties": ch.properties,
                    "descriptors": [],
                }
                skip = ch.uuid.startswith(SKIP_PREFIXES)
                if "read" in ch.properties and not skip:
                    try:
                        data = bytes(await client.read_gatt_char(ch))
                        c["value_hex"] = data.hex()
                        c["value_decoded"] = decode(ch.uuid, data)
                    except Exception as e:
                        c["read_error"] = str(e)
                for d in ch.descriptors:
                    dd = {"uuid": d.uuid, "handle": d.handle, "description": d.description}
                    if not skip:
                        try:
                            dd["value_hex"] = bytes(await client.read_gatt_descriptor(d.handle)).hex()
                        except Exception as e:
                            dd["read_error"] = str(e)
                    c["descriptors"].append(dd)
                s["characteristics"].append(c)

                val = c.get("value_decoded") or c.get("value_hex") or c.get("read_error") or ""
                print(f"  [{ch.handle:>3}] {ch.uuid}  ({','.join(ch.properties)})  {ch.description}")
                if val:
                    print(f"        value: {val}")
                for dd in c["descriptors"]:
                    print(f"        desc {dd['uuid'][:8]} = {dd.get('value_hex', dd.get('read_error', ''))}")
            out["services"].append(s)

    path = Path("captures") / f"gatt-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(f"\nSaved {path}")


if __name__ == "__main__":
    asyncio.run(main())
