"""Hardware smoke test of the kingsmith_r3 library's control path. MOVES THE BELT.

start -> 2.0 -> 3.0 -> above-limit request (expect clamp) -> pause -> resume -> stop (end session)
-> start then stop() during the 3-2-1 countdown.
Each step is verified against live Treadmill Data. Any failure pauses the belt.

Usage: uv run tools/library_smoke.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kingsmith_r3 import WalkingPad  # noqa: E402

T0 = time.monotonic()
results: list[tuple[str, bool, str]] = []


def say(msg):
    print(f"{time.monotonic() - T0:7.2f}  {msg}", flush=True)


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    say(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        raise AssertionError(name)


async def settle_at(pad, kmh, timeout=12):
    ok = await pad.wait_for(lambda: abs(pad.data.speed_kmh - kmh) < 0.05, timeout)
    return ok, f"belt reports {pad.data.speed_kmh:.1f} km/h"


async def main():
    events = []
    async with WalkingPad() as pad:
        pad.on_machine_status = lambda e: events.append(e)
        say(f"connected: limit {pad.max_speed_kmh} km/h, handle_up={pad.properties.handle_up}, "
            f"supplement={'unlocked' if pad.supplement_ready else 'locked'}")
        check("supplement handshake", pad.supplement_ready)
        try:
            if pad.data.elapsed_s and not pad.data.moving:
                await pad.stop()  # end any pending paused session so counters start at 0
                say("ended pending session")

            await pad.start()
            check("start() -> belt moving", pad.data.moving, f"{pad.data.speed_kmh:.1f} km/h")

            got = await pad.set_speed(2.0)
            check("set_speed(2.0)", *(await settle_at(pad, got)))

            got = await pad.set_speed(3.0)
            check("set_speed(3.0)", *(await settle_at(pad, got)))

            over = pad.max_speed_kmh + 3
            got = await pad.set_speed(over)
            check(f"set_speed({over}) clamps to limit", got == pad.max_speed_kmh, f"requested {got}")
            check("belt reaches limit", *(await settle_at(pad, got, timeout=15)))
            await asyncio.sleep(2)

            await pad.set_speed(2.0)
            await settle_at(pad, 2.0, 15)

            await pad.pause()
            check("pause() -> belt stopped", not pad.data.moving, f"{pad.data.speed_kmh:.1f} km/h")
            frozen = pad.data.elapsed_s
            await asyncio.sleep(3)
            check("elapsed frozen while paused", pad.data.elapsed_s == frozen, f"{frozen}s")

            await pad.start()
            check("start() resumes", pad.data.moving and pad.data.elapsed_s >= frozen,
                  f"{pad.data.speed_kmh:.1f} km/h, elapsed {pad.data.elapsed_s}s")
            await asyncio.sleep(3)

            await pad.stop()
            ok = await pad.wait_for(lambda: pad.data.elapsed_s == 0 and not pad.data.moving, 5)
            check("stop() ends session, counters reset", ok, f"{pad.data}")

            # START can't be cancelled during its 3-2-1 countdown: PAUSE/STOP
            # are refused and the belt starts anyway. stop() must catch it.
            await asyncio.sleep(2)
            await pad.start(wait=False)
            await asyncio.sleep(1.5)
            check("countdown under way", pad.starting and not pad.data.moving)
            await pad.stop()
            ok = await pad.wait_for(lambda: pad.data.elapsed_s == 0 and not pad.data.moving, 5)
            check("stop() during countdown halts belt and ends session", ok, f"{pad.data}")
            await asyncio.sleep(4)
            check("belt stays stopped", not pad.data.moving and not pad.starting, f"{pad.data}")
        except BaseException:
            say("!! failure -> pausing")
            try:
                await pad.pause(wait=False)
            except Exception as e:  # noqa: BLE001
                say(f"!! pause failed too: {e}")
            raise
        say(f"machine status events seen: {[hex(e.event) for e in events]}")

    print("\nSUMMARY")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


if __name__ == "__main__":
    asyncio.run(main())
