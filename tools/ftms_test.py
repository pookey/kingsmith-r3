"""Scripted FTMS control test. Subscribes to everything (logging like sniff.py),
then runs a slow start / speed / pause / resume / stop sequence.

Usage: uv run tools/ftms_test.py [--name KS-AP-RQ3]
"""

import argparse
import asyncio
import json
import struct
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

from sniff import SKIP_PREFIXES, decode, label

CP = "00002ad9-0000-1000-8000-00805f9b34fb"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="KS-AP-RQ3")
    args = ap.parse_args()

    dev = await BleakScanner.find_device_by_name(args.name, timeout=15)
    if not dev:
        raise SystemExit("Device not found")

    path = Path("captures") / f"ftms-test-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    f = path.open("w")
    t0 = time.monotonic()
    state = {"speed": 0.0, "last_td": None}

    def log(rec):
        rec["t"] = round(time.monotonic() - t0, 3)
        f.write(json.dumps(rec) + "\n")
        f.flush()

    def handler_for(uuid):
        def h(_s, data):
            b = bytes(data)
            dec = decode(uuid, b)
            log({"dir": "rx", "char": label(uuid), "hex": b.hex(), "decoded": dec})
            if uuid.startswith("00002acd"):
                state["speed"] = (dec or {}).get("speed", 0.0)
                if b.hex() == state["last_td"]:
                    return
                state["last_td"] = b.hex()
            print(f"{time.monotonic() - t0:8.3f}  <- {label(uuid):<15} {b.hex(' ')}  {dec or ''}", flush=True)
        return h

    async def cp(payload: bytes, what: str):
        print(f"{time.monotonic() - t0:8.3f}  -> CP {payload.hex(' ')}   [{what}]", flush=True)
        log({"dir": "tx", "char": "ControlPoint", "hex": payload.hex(), "what": what})
        try:
            await client.write_gatt_char(CP, payload, response=True)
        except Exception as e:
            print(f"          write error: {e}", flush=True)
            log({"dir": "err", "what": what, "error": str(e)})

    def speed_cmd(kmh):
        return bytes([0x02]) + struct.pack("<H", round(kmh * 100))

    async def wait_moving(timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if state["speed"] > 0:
                return True
            await asyncio.sleep(0.2)
        return False

    async def step(sec, note):
        print(f"          ... waiting {sec}s ({note})", flush=True)
        await asyncio.sleep(sec)

    async with BleakClient(dev, timeout=20, disconnected_callback=lambda _: print("!! DISCONNECTED", flush=True)) as client:
        print(f"Connected. Logging to {path}", flush=True)
        for svc in client.services:
            for ch in svc.characteristics:
                if ch.uuid.startswith(SKIP_PREFIXES):
                    continue
                if {"notify", "indicate"} & set(ch.properties):
                    await client.start_notify(ch, handler_for(ch.uuid))
                    await asyncio.sleep(0.3)
        await step(2, "settle")

        try:
            await cp(b"\x00", "REQUEST_CONTROL")
            await step(2, "after request control")

            await cp(b"\x07", "START_OR_RESUME")
            moving = await wait_moving()
            print(f"          belt moving: {moving}", flush=True)
            await step(4, "stabilise")

            await cp(speed_cmd(2.0), "SET_TARGET_SPEED 2.0")
            await step(8, "at 2.0")

            await cp(speed_cmd(1.5), "SET_TARGET_SPEED 1.5")
            await step(6, "at 1.5")

            await cp(b"\x08\x02", "PAUSE")
            await step(6, "paused")

            await cp(b"\x07", "RESUME")
            await wait_moving()
            await step(6, "resumed")

            await cp(b"\x08\x01", "STOP")
            await step(8, "observe stop")
        finally:
            if client.is_connected and state["speed"] > 0:
                await cp(b"\x08\x01", "SAFETY STOP")
                await asyncio.sleep(2)
    f.close()
    print(f"Done. Saved {path}")


if __name__ == "__main__":
    asyncio.run(main())
