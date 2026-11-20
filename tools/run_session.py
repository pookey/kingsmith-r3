"""Run: start -> max speed for N s -> slow to walk speed -> wait for user to stop -> report.

Usage: uv run tools/run_session.py [--hold 30] [--walk 4.0] [--max-wait 900]
"""

import argparse
import asyncio
import json
import struct
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

from sniff import decode

TD = "00002acd-0000-1000-8000-00805f9b34fb"
TS = "00002ad3-0000-1000-8000-00805f9b34fb"
CP = "00002ad9-0000-1000-8000-00805f9b34fb"
FMS = "00002ada-0000-1000-8000-00805f9b34fb"
RANGE = "00002ad4-0000-1000-8000-00805f9b34fb"
SUPP_N = "24e2521c-f63b-48ed-85be-c5330b00fdf7"
SUPP_W = "24e2521c-f63b-48ed-85be-c5330d00fdf7"
TOKEN = bytes.fromhex("710005" "87b4525133" "87")  # replayed KS Fit handshake
GET_PROPS = bytes.fromhex("7200000072")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="KS-AP-RQ3")
    ap.add_argument("--hold", type=float, default=30)
    ap.add_argument("--walk", type=float, default=4.0)
    ap.add_argument("--dry", action="store_true", help="connect, subscribe, handshake, read props, listen 5s; never start")
    ap.add_argument("--max-wait", type=float, default=900, help="give up waiting for user stop after N s")
    args = ap.parse_args()

    dev = await BleakScanner.find_device_by_name(args.name, timeout=15)
    if not dev:
        raise SystemExit("Device not found")

    path = Path("captures") / f"session-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    f = path.open("w")
    t0 = time.monotonic()
    st = {"td": None, "props": None, "events": []}

    def now():
        return time.monotonic() - t0

    def log(rec):
        rec["t"] = round(now(), 3)
        f.write(json.dumps(rec, default=str) + "\n")
        f.flush()

    def say(msg):
        print(f"{now():8.2f}  {msg}", flush=True)

    def handler(uuid, name):
        def h(_s, d):
            try:
                _h(_s, d)
            except Exception as e:  # bleak swallows callback exceptions; surface them
                import traceback
                say(f"!! handler error in {name}: {e!r}")
                traceback.print_exc()

        def _h(_s, d):
            b = bytes(d)
            dec = decode(uuid, b)
            log({"dir": "rx", "char": name, "hex": b.hex(), "decoded": dec})
            if name == "TreadmillData":
                prev = st["td"]
                st["td"] = dec
                if not prev or (prev.get("speed"), prev.get("steps") // 10) != (dec.get("speed"), dec.get("steps") // 10):
                    say(f"TD speed={dec['speed']:.2f} t={dec['elapsed_s']}s dist={dec['dist_m']}m "
                        f"kcal={dec['kcal']} steps={dec['steps']}")
            else:
                if name == "Supplement" and dec and dec.get("what") == "property_list":
                    st["props"] = dec["props"]
                st["events"].append((now(), name, b.hex()))
                say(f"<- {name} {b.hex(' ')}  {dec or ''}")
        return h

    async def cp(payload, what):
        say(f"-> CP {payload.hex(' ')} [{what}]")
        log({"dir": "tx", "char": "ControlPoint", "hex": payload.hex(), "what": what})
        await client.write_gatt_char(CP, payload, response=True)

    async def control(payload, what):
        # KS Fit re-requests control before every command; do the same.
        await cp(b"\x00", "REQUEST_CONTROL")
        await asyncio.sleep(0.2)
        await cp(payload, what)

    def speed():
        return (st["td"] or {}).get("speed", 0.0)

    async def wait_until(pred, timeout, step=0.2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            await asyncio.sleep(step)
        return False

    async with BleakClient(dev, timeout=20, disconnected_callback=lambda _: say("!! DISCONNECTED")) as client:
        say(f"Connected. Logging to {path}")
        mn, mx, inc = struct.unpack("<HHH", bytes(await client.read_gatt_char(RANGE)))
        say(f"Speed range {mn/100}-{mx/100} step {inc/100}")
        subs = [(TD, "TreadmillData"), (TS, "TrainingStatus"), (CP, "ControlPoint"),
                (FMS, "FMStatus"), (SUPP_N, "Supplement")]
        # The pad sometimes ignores CCCD writes (KS Fit rewrites them 3x). Verify
        # Treadmill Data (1 Hz) actually flows; resubscribe until it does.
        for attempt in range(1, 5):
            for uuid, name in subs:
                if attempt > 1:
                    try:
                        await client.stop_notify(uuid)
                    except Exception:
                        pass
                await client.start_notify(uuid, handler(uuid, name))
                await asyncio.sleep(0.2)
            if await wait_until(lambda: st["td"] is not None, 2.5):
                say(f"Telemetry flowing (subscribe attempt {attempt})")
                break
            say(f"!! no Treadmill Data after subscribe attempt {attempt}, retrying")
        else:
            raise SystemExit("Telemetry never started - not touching the belt")

        for attempt in range(1, 4):
            await client.write_gatt_char(SUPP_W, TOKEN, response=True)
            await asyncio.sleep(0.4)
            await client.write_gatt_char(SUPP_W, GET_PROPS, response=True)
            if await wait_until(lambda: st["props"] is not None, 2):
                break
        limit = (st["props"] or {}).get("maxLimitSpeed")
        say(f"maxLimitSpeed property = {limit}")
        target = min(mx / 100, limit / 10) if limit else mx / 100
        say(f"Target max speed = {target:.1f} km/h")

        if args.dry:
            await asyncio.sleep(5)
            say(f"DRY RUN: last TD = {st['td']}, events = {len(st['events'])}")
            return

        try:
            if st["td"] is None:
                raise RuntimeError("no Treadmill Data received - refusing to start")
            if speed() == 0 and st["td"].get("elapsed_s", 0) > 0:
                # A paused session is pending; STOP (only accepted when paused) ends it so counters reset.
                await control(b"\x08\x01", "END PENDING SESSION")
                await wait_until(lambda: st["td"].get("elapsed_s", 1) == 0, 5)
                say(f"Counters after end: {st['td']}")
            await control(b"\x07", "START")
            if not await wait_until(lambda: speed() > 0, 20):
                raise RuntimeError("belt did not start")
            await asyncio.sleep(3)

            await control(b"\x02" + struct.pack("<H", round(target * 100)), f"SET SPEED {target:.1f}")
            # Wait for the belt to reach target, or plateau (if something caps it).
            last, last_change = speed(), time.monotonic()
            while time.monotonic() - last_change < 4 and speed() < target - 0.05:
                await asyncio.sleep(0.3)
                if speed() != last:
                    last, last_change = speed(), time.monotonic()
            reached = speed()
            say(f"=== Reached {reached:.1f} km/h (target {target:.1f}); holding {args.hold:.0f}s ===")
            hold_start = time.monotonic()
            samples = []
            while time.monotonic() - hold_start < args.hold:
                samples.append(speed())
                await asyncio.sleep(0.5)
            say(f"=== Hold done: min {min(samples):.1f} / max {max(samples):.1f} km/h over {args.hold:.0f}s ===")

            await control(b"\x02" + struct.pack("<H", round(args.walk * 100)), f"SET SPEED {args.walk}")
            await wait_until(lambda: abs(speed() - args.walk) < 0.05, 20)
            say(f"=== At {speed():.1f} km/h. User can step on. Waiting for user stop... ===")
            steps_at_walk = (st["td"] or {}).get("steps", 0)

            stopped = await wait_until(lambda: speed() == 0, args.max_wait, step=0.5)
            await asyncio.sleep(2)  # let final frames arrive
            td = st["td"] or {}
            say(f"=== {'Belt stopped by user' if stopped else 'Timed out waiting'} ===")
            say(f"FINAL: steps={td.get('steps')} (were {steps_at_walk} when slowed to walk) "
                f"elapsed={td.get('elapsed_s')}s dist={td.get('dist_m')}m kcal={td.get('kcal')}")
            log({"summary": td, "reached_max": reached, "target": target, "steps_at_walk": steps_at_walk})
        except BaseException:
            # Always pause on error: if notifications are broken, speed() is meaningless.
            if client.is_connected:
                say("!! error -> PAUSE")
                await control(b"\x08\x02", "SAFETY PAUSE")
                await asyncio.sleep(2)
            raise
    f.close()


if __name__ == "__main__":
    asyncio.run(main())
