"""Passive notification logger. Subscribes (staggered) to every notify/indicate
characteristic and logs raw hex + FTMS decoding to captures/*.jsonl.
Sends NOTHING except CCCD enables.

Usage: uv run tools/sniff.py [--name KS-AP-RQ3] [--duration 120] [--quiet-ftms]
"""

import argparse
import asyncio
import json
import struct
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

SKIP_PREFIXES = ("32e2314c-",)

LABELS = {
    "00002acd": "TreadmillData",
    "00002ad3": "TrainingStatus",
    "00002ada": "FMStatus",
    "00002ad9": "ControlPoint",
    "24e2521c-f63b-48ed-85be-c5330b00fdf7": "Supplement",
    "5833ff03-9b8b-5191-6142-22a4536ef123": "Vendor5833",
}

FMS_EVENTS = {
    0x01: "reset", 0x02: "stopped_or_paused", 0x03: "stopped_safety_key", 0x04: "started_or_resumed",
    0x05: "target_speed_changed", 0x06: "target_incline_changed", 0xFF: "control_lost",
}
TRAINING = {
    0x00: "other", 0x01: "idle", 0x02: "warming_up", 0x0C: "manual_mode",
    0x0D: "pre_workout", 0x0E: "post_workout",
}


def label(uuid):
    return LABELS.get(uuid) or LABELS.get(uuid[:8]) or uuid


def parse_treadmill(b: bytes):
    flags = struct.unpack_from("<H", b)[0]
    o, r = 2, {"flags": f"0x{flags:04x}"}

    def take(fmt):
        nonlocal o
        v = struct.unpack_from(fmt, b, o)
        o += struct.calcsize(fmt)
        return v

    def u24():
        nonlocal o
        v = b[o] | b[o + 1] << 8 | b[o + 2] << 16
        o += 3
        return v

    try:
        if not flags & 0x0001:
            r["speed"] = take("<H")[0] / 100
        if flags & 0x0002:
            r["avg_speed"] = take("<H")[0] / 100
        if flags & 0x0004:
            r["dist_m"] = u24()
        if flags & 0x0008:
            r["incl"], r["ramp"] = take("<hh")
        if flags & 0x0010:
            r["elev_pos"], r["elev_neg"] = take("<HH")
        if flags & 0x0020:
            r["inst_pace"] = take("<B")[0]
        if flags & 0x0040:
            r["avg_pace"] = take("<B")[0]
        if flags & 0x0080:
            r["kcal"], r["kcal_h"], r["kcal_min"] = take("<HHB")
        if flags & 0x0100:
            r["hr"] = take("<B")[0]
        if flags & 0x0200:
            r["met"] = take("<B")[0]
        if flags & 0x0400:
            r["elapsed_s"] = take("<H")[0]
        if flags & 0x0800:
            r["remaining_s"] = take("<H")[0]
        if flags & 0x1000:
            r["force"], r["power"] = take("<hh")
        if flags & 0x2000:
            r["steps"] = u24()
    except struct.error:
        r["truncated_at"] = o
    if o < len(b):
        r["trailing"] = b[o:].hex()
    return r


PROPS = {  # KS Fit FTMSPropertyType ids
    1: "unit", 2: "autoStop", 3: "realTimePace", 4: "motorVersion", 5: "errCode", 6: "childLock",
    7: "maxLimitSpeed", 8: "otherSwitch", 9: "readOnly", 10: "modeStatus", 11: "maxSpeed",
    12: "zoneTime", 13: "lightColor",
}


def _ts(v):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v)) if v else None


def supplement_detail(cmd, flags, p: bytes):
    """Best-effort meaning of KingSmith Supplement frames (see docs/device-ks-r3q.md)."""
    try:
        if cmd == 0x71 and flags == 0x01 and len(p) >= 4:
            return {"what": "time_sync", "time": _ts(struct.unpack_from("<I", p)[0]), "extra": p[4:].hex()}
        if cmd == 0x71 and flags == 0x00:
            return {"what": "handshake_token"}
        if cmd == 0x72 and flags == 0x80 and len(p) % 4 == 0:
            props = {}
            for i in range(0, len(p), 4):
                pid, val = struct.unpack_from("<HH", p, i)
                props[PROPS.get(pid, f"p{pid}")] = val
            return {"what": "property_list", "props": props}
        if cmd == 0x72 and flags == 0x01 and len(p) == 3:
            return {"what": "set_property", "prop": PROPS.get(p[0], p[0]), "value": struct.unpack_from("<H", p, 1)[0]}
        if cmd == 0x73 and len(p) >= 13:
            start, x, end = struct.unpack_from("<III", p, 1)
            return {"what": "session_record", "start": _ts(start), "x": x, "end": _ts(end)}
    except struct.error:
        pass
    return {}


def decode(uuid, b: bytes):
    s = uuid[:8]
    if not b:
        return None
    if s == "00002acd":
        return parse_treadmill(b)
    if s == "00002ada":
        d = {"event": FMS_EVENTS.get(b[0], f"0x{b[0]:02x}")}
        if b[0] == 0x05 and len(b) >= 3:
            d["speed"] = struct.unpack_from("<H", b, 1)[0] / 100
        elif len(b) > 1:
            d["param"] = b[1:].hex()
        return d
    if s == "00002ad3" and len(b) >= 2:
        return {"flags": b[0], "status": TRAINING.get(b[1], f"0x{b[1]:02x}"), "rest": b[2:].hex() or None}
    if s == "00002ad9" and b[0] == 0x80 and len(b) >= 3:
        return {"response_to": f"0x{b[1]:02x}", "result": b[2]}
    if uuid.startswith("24e2521c") and len(b) >= 4:
        # KingSmith Supplement frame: [cmd][flags][len][payload*len][chk]
        n = b[2]
        d = {"cmd": f"0x{b[0]:02x}", "flags": f"0x{b[1]:02x}", "len": n, "payload": b[3:3 + n].hex()}
        if len(b) == n + 4:
            if b[-1] == sum(b[:-1]) & 0xFF:
                d["chk"] = "ok(sum all)"
            elif b[-1] == sum(b[1:-1]) & 0xFF:
                d["chk"] = "ok(sum excl cmd)"
            else:
                d["chk"] = f"BAD {b[-1]:02x}"
        else:
            d["len_mismatch"] = len(b)
        d.update(supplement_detail(b[0], b[1], b[3:3 + n]))
        return d
    if all(32 <= x < 127 or x in (10, 13) for x in b):
        return {"ascii": b.decode()}
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="KS-AP-RQ3")
    ap.add_argument("--duration", type=float, default=120)
    ap.add_argument("--quiet-ftms", action="store_true", help="only print TreadmillData when it changes")
    args = ap.parse_args()

    dev = await BleakScanner.find_device_by_name(args.name, timeout=15)
    if not dev:
        raise SystemExit("Device not found")

    path = Path("captures") / f"sniff-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    path.parent.mkdir(exist_ok=True)
    f = path.open("w")
    t0 = time.monotonic()
    last_td = {}

    def handler_for(uuid):
        def h(_sender, data: bytearray):
            b = bytes(data)
            t = round(time.monotonic() - t0, 3)
            dec = decode(uuid, b)
            rec = {"t": t, "wall": time.time(), "char": label(uuid), "uuid": uuid, "hex": b.hex(), "decoded": dec}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            if args.quiet_ftms and uuid.startswith("00002acd"):
                key = b.hex()
                if last_td.get("k") == key:
                    return
                last_td["k"] = key
            print(f"{t:8.3f}  {label(uuid):<15} {b.hex(' ')}  {dec if dec else ''}", flush=True)
        return h

    async with BleakClient(dev, timeout=20, disconnected_callback=lambda _: print("!! DISCONNECTED", flush=True)) as client:
        print(f"Connected to {dev.name}. Logging to {path}", flush=True)
        for svc in client.services:
            for ch in svc.characteristics:
                if ch.uuid.startswith(SKIP_PREFIXES):
                    continue
                if {"notify", "indicate"} & set(ch.properties):
                    try:
                        await client.start_notify(ch, handler_for(ch.uuid))
                        print(f"  subscribed {label(ch.uuid)}", flush=True)
                    except Exception as e:
                        print(f"  subscribe FAILED {label(ch.uuid)}: {e}", flush=True)
                    await asyncio.sleep(0.3)  # firmware drops rapid CCCD writes
        print(f"Listening for {args.duration:.0f}s ...", flush=True)
        end = time.monotonic() + args.duration
        while time.monotonic() < end and client.is_connected:
            await asyncio.sleep(0.5)
    f.close()
    print(f"Done. Saved {path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
