"""Probe the KingSmith Supplement channel (24e2521c-...). No belt commands.

Sets up FTMS like KS Fit (subs + REQUEST_CONTROL), then runs --steps.

Usage: uv run tools/supp_probe.py --steps props,hs:87b4525133,props,set7:65,props
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

from sniff import decode

NOTIFY = "24e2521c-f63b-48ed-85be-c5330b00fdf7"
WRITE = "24e2521c-f63b-48ed-85be-c5330d00fdf7"
FMS = "00002ada-0000-1000-8000-00805f9b34fb"
TD = "00002acd-0000-1000-8000-00805f9b34fb"
TS = "00002ad3-0000-1000-8000-00805f9b34fb"
CP = "00002ad9-0000-1000-8000-00805f9b34fb"


def frame(cmd, flags, payload=b""):
    body = bytes([cmd, flags, len(payload)]) + payload
    return body + bytes([sum(body) & 0xFF])


GET_PROPS = bytes.fromhex("7200000072")  # verbatim from KS Fit (note the extra 00)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="KS-AP-RQ3")
    ap.add_argument("--steps", default="props,hs:0000000000,props",
                    help="comma list: props | hs:<10 hex> | set7:<int> | time")
    args = ap.parse_args()

    dev = await BleakScanner.find_device_by_name(args.name, timeout=15)
    if not dev:
        raise SystemExit("Device not found (is KS Fit still connected?)")

    path = Path("captures") / f"supp-probe-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    f = path.open("w")
    t0 = time.monotonic()
    rx: asyncio.Queue = asyncio.Queue()

    def log(rec):
        rec["t"] = round(time.monotonic() - t0, 3)
        f.write(json.dumps(rec) + "\n")
        f.flush()

    def on_supp(_s, d):
        b = bytes(d)
        dec = decode(NOTIFY, b)
        log({"dir": "rx", "char": "Supplement", "hex": b.hex(), "decoded": dec})
        print(f"{time.monotonic() - t0:7.3f}  <- {b.hex(' ')}\n           {dec}", flush=True)
        rx.put_nowait(b)

    def on_cp(_s, d):
        b = bytes(d)
        log({"dir": "rx", "char": "ControlPoint", "hex": b.hex()})
        print(f"{time.monotonic() - t0:7.3f}  <- CP {b.hex(' ')}", flush=True)

    def on_fms(_s, d):
        b = bytes(d)
        log({"dir": "rx", "char": "FMStatus", "hex": b.hex()})
        print(f"{time.monotonic() - t0:7.3f}  <- FMStatus {b.hex(' ')}", flush=True)

    async def send(b: bytes, what: str, wait=2.0):
        while not rx.empty():
            rx.get_nowait()
        print(f"\n{time.monotonic() - t0:7.3f}  -> {b.hex(' ')}   [{what}]", flush=True)
        log({"dir": "tx", "char": "Supplement", "hex": b.hex(), "what": what})
        await client.write_gatt_char(WRITE, b, response=True)
        try:
            return await asyncio.wait_for(rx.get(), wait)
        except asyncio.TimeoutError:
            print("           (no response)", flush=True)
            return None

    async with BleakClient(dev, timeout=20) as client:
        print(f"Connected. Logging to {path}")
        # Mirror KS Fit's setup order (capture 3/4): FTMS subs, then Supplement, then CP 00.
        for uuid, h in [(TD, None), (TS, None), (CP, on_cp), (FMS, on_fms), (NOTIFY, on_supp)]:
            await client.start_notify(uuid, h or (lambda _s, _d: None))
            await asyncio.sleep(0.2)
        await client.write_gatt_char(CP, b"\x00", response=True)
        await asyncio.sleep(1.0)

        for step in args.steps.split(","):
            if step == "props":
                await send(GET_PROPS, "get props")
            elif step.startswith("hs:"):
                tok = step[3:]
                await send(frame(0x71, 0x00, bytes.fromhex(tok)), f"handshake token={tok}")
            elif step.startswith("set7:"):
                v = int(step[5:])
                await send(frame(0x72, 0x01, bytes([7]) + v.to_bytes(2, "little")), f"set maxLimitSpeed={v}")
            elif step.startswith("raw:"):  # raw:<cmd><flags><payload hex>; len + chk added
                body = bytes.fromhex(step[4:])
                await send(frame(body[0], body[1], body[2:]), f"raw {step[4:]}")
            elif step == "time":
                now = int(time.time()).to_bytes(4, "little")
                await send(frame(0x71, 0x01, now + bytes([1, 0, 0, 0])), "time sync")
        await asyncio.sleep(1.0)
    f.close()
    print(f"\nDone. Saved {path}")


if __name__ == "__main__":
    asyncio.run(main())
