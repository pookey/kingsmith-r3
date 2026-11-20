"""Command line: ``kingsmith-r3 {scan,info,watch,start,speed,pause,stop}``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from . import protocol as p
from .client import WalkingPad, discover


def _fmt(d: p.TreadmillData) -> str:
    return (f"speed {d.speed_kmh:5.2f} km/h  time {d.elapsed_s:4d}s  dist {d.distance_m:5d} m  "
            f"kcal {d.kcal:3d}  steps {d.steps:5d}")


async def _scan(args) -> int:
    devs = await discover(args.timeout)
    for d in devs:
        print(f"{d.name}\t{d.address}")
    if not devs:
        print("No KS-AP-* devices found.", file=sys.stderr)
        return 1
    return 0


async def _info(pad: WalkingPad, args) -> None:
    r = pad.speed_range
    print(f"speed range : {r.min_kmh}-{r.max_kmh} km/h, step {r.step_kmh}" if r else "speed range : ?")
    print(f"speed limit : {pad.max_speed_kmh} km/h (handle {'up' if pad.properties.handle_up else 'down'})")
    print(f"supplement  : {'unlocked' if pad.supplement_ready else 'locked'}")
    for k, v in pad.properties.named().items():
        print(f"  {k:<16} {v} (0x{v:04x})")
    print(f"now         : {_fmt(pad.data)}")


async def _watch(pad: WalkingPad, args) -> None:
    def show(d: p.TreadmillData) -> None:
        if args.json:
            print(json.dumps({k: v for k, v in vars(d).items() if k != "raw"}), flush=True)
        else:
            print(_fmt(d), flush=True)

    def event(e: p.MachineStatus) -> None:
        if not args.json:
            name = p.MachineEvent(e.event).name if e.event in p.MachineEvent._value2member_map_ else hex(e.event)
            extra = f" -> {e.target_speed_kmh} km/h" if e.target_speed_kmh is not None else ""
            print(f"[event] {name}{extra}", flush=True)

    pad.on_data, pad.on_machine_status = show, event
    pad.pause_on_exit = False  # watching must never change belt state
    show(pad.data)
    await asyncio.sleep(args.seconds if args.seconds else 10**9)


async def _start(pad: WalkingPad, args) -> None:
    pad.pause_on_exit = False
    await pad.start()
    print(_fmt(pad.data))


async def _speed(pad: WalkingPad, args) -> None:
    pad.pause_on_exit = False
    got = await pad.set_speed(args.kmh)
    print(f"target {got} km/h")


async def _pause(pad: WalkingPad, args) -> None:
    await pad.pause()
    print(_fmt(pad.data))


async def _stop(pad: WalkingPad, args) -> None:
    before = pad.data
    await pad.stop()
    print(f"session ended: {_fmt(before)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kingsmith-r3", description="Control a KingSmith R3 walking pad over BLE.")
    ap.add_argument("--name", default=p.DEFAULT_NAME, help="BLE name (default %(default)s)")
    ap.add_argument("--address", help="BLE address/UUID (skips scanning by name)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="list R3 pads in range")
    s.add_argument("--timeout", type=float, default=8.0)
    sub.add_parser("info", help="speed range, limit, properties, current data")
    w = sub.add_parser("watch", help="stream live data (read-only)")
    w.add_argument("--json", action="store_true")
    w.add_argument("--seconds", type=float, default=0)
    sub.add_parser("start", help="start/resume the belt")
    sp = sub.add_parser("speed", help="set speed in km/h (starts the belt if stopped)")
    sp.add_argument("kmh", type=float)
    sub.add_parser("pause", help="pause the belt (safe halt)")
    sub.add_parser("stop", help="end the session (pauses first)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    if args.cmd == "scan":
        return asyncio.run(_scan(args))

    handlers = {"info": _info, "watch": _watch, "start": _start, "speed": _speed, "pause": _pause, "stop": _stop}

    async def run() -> int:
        # watch skips the handshake so the pad keeps streaming at 1 Hz while idle
        async with WalkingPad(args.address, name=args.name, supplement=args.cmd != "watch") as pad:
            await handlers[args.cmd](pad, args)
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
