# kingsmith-r3

Python library and CLI for the **KingSmith R3 walking pad** over Bluetooth LE. It also documents the pad's BLE protocol, which was reverse-engineered from the device and from the official KS Fit app's traffic.

| | |
|---|---|
| BLE name | `KS-AP-RQ3` |
| Model | `KS-R3Q` (manufacturer string "3Q") |
| Firmware tested | `V74.02.23` / software `V0.0.5` |

The R3 doesn't appear in any of the existing WalkingPad projects (see [docs/RESEARCH.md](docs/RESEARCH.md)). It speaks:

- **Standard FTMS** (Bluetooth Fitness Machine Service) for live data and belt control.
- **KingSmith's "Supplement" channel**, which needs a handshake, for device properties (speed limit / handle position, units, child lock …), clock sync and session records.

Full details are in **[docs/PROTOCOL.md](docs/PROTOCOL.md)**.

## Install

```bash
pip install git+https://github.com/pookey/kingsmith-r3
# or, from a clone:
uv sync && uv run kingsmith-r3 --help
```

Requires Python ≥ 3.10 and [bleak](https://github.com/hbldh/bleak). It's tested on macOS (CoreBluetooth); Linux/BlueZ and Windows should work through bleak.

> **The pad accepts one BLE connection at a time.** Close or force-quit the KS Fit app first, or the pad won't advertise. On macOS, your terminal needs Bluetooth permission (System Settings → Privacy & Security → Bluetooth).

## CLI

```bash
kingsmith-r3 scan            # find KS-AP-* pads
kingsmith-r3 info            # speed range, current limit / handle position, properties, live data
kingsmith-r3 watch           # stream live data and remote-control events (read-only)
kingsmith-r3 watch --json    # … as JSON lines
kingsmith-r3 start           # start / resume (3-2-1 countdown, then 1.0 km/h)
kingsmith-r3 speed 3.5       # set speed (starts the belt if needed)
kingsmith-r3 pause           # pause: belt ramps down, session kept. The safe halt.
kingsmith-r3 stop            # end the session (pauses first); counters reset
```

## Library

```python
import asyncio
from kingsmith_r3 import WalkingPad

async def main():
    async with WalkingPad() as pad:              # scans for KS-AP-RQ3, handshakes, reads properties
        print(pad.max_speed_kmh, pad.properties.handle_up)
        pad.on_data = lambda d: print(d.speed_kmh, d.steps, d.distance_m, d.kcal)
        pad.on_machine_status = lambda e: print("event", e)   # e.g. remote +/- → target_speed_kmh

        await pad.start()            # waits until the belt is moving
        await pad.set_speed(4.0)
        await asyncio.sleep(60)
        await pad.stop()             # pause → wait for 0 km/h → end session
        # leaving the block pauses the belt if it's still moving

asyncio.run(main())
```

`pad.data` is the latest `TreadmillData` (speed, distance, kcal, elapsed time, steps). Other things you can use:

- **Callbacks:** `on_data`, `on_machine_status`, `on_training_status`, `on_session_record`, `on_disconnect`.
- **Methods:** `refresh_properties()`, `sync_clock()` and `handshake(token)`.

## Safety

**The belt can be started remotely. Make sure it's clear, and stay in reach of the remote.**

- The pad has **no application-level watchdog**. If your program crashes or disconnects while the belt runs, **the belt keeps running**. `WalkingPad` pauses on exiting the `async with` block, but it can't help if the process is killed.
- **STOP is refused while the belt is moving.** You must PAUSE first, which `pad.stop()` does for you. A raw STOP while running returns `OPERATION_FAILED` and the belt keeps going.
- **A start can't be cancelled during its 3-2-1 countdown.** The pad refuses both PAUSE and STOP, then starts the belt at 1 km/h anyway. `pad.pause()`, `pad.stop()` and leaving the `async with` block detect the countdown (your own START, or one from the remote or app), wait for the belt to move, and pause it straight away. On the R3 the belt runs for about a second.
- The pad occasionally drops a command's confirmation even though the command worked. The client then checks the live data, and only raises an error if the command had no visible effect.
- After the Supplement handshake, the pad **stops sending live data while idle**. It only sends data while the belt moves. `WalkingPad(supplement=False)` skips the handshake and keeps the 1 Hz stream; `watch` does this.
- After connecting, the pad sometimes ignores subscription (CCCD) writes, so no data arrives. The client checks that telemetry is flowing and resubscribes. Never drive the belt on a connection you aren't receiving data on, because you can't confirm it stopped.

## Key findings (short version)

- **Speed limit follows the handle.** It's 6.0 km/h with the handle down and 12.0 km/h with it up. The limit is reported as property 7, and bit `0x08` of property 9 tracks the handle. Neither can be written over BLE.
- **Distance, kcal and steps only count while someone is walking** (foot sensor). Elapsed time counts whenever the belt runs.
- **The remote** reports +/- presses as FTMS "target speed changed" events, in 0.5 km/h steps. Its stop button is a *pause*.
- **The Supplement channel** ignores everything until it gets a valid 5-byte handshake token. Tokens captured from KS Fit keep working across connections, and a token with any byte altered is ignored. The library ships the two captured tokens. **If they don't work on your R3, please open an issue.** It would mean tokens are per-device.

## Reverse-engineering tools

`tools/` contains the scripts used to work all this out:

| Script | Purpose |
|---|---|
| `scan.py` | Passive scan for candidate devices |
| `gatt_dump.py` | Read-only GATT dump |
| `sniff.py` | Passive notification logger |
| `pklg_parse.py` | Decoder for Apple PacketLogger iPhone captures |
| `supp_probe.py` | Supplement-channel probe |
| `ftms_test.py`, `run_session.py`, `halt.py` | Scripted control and halting |
| `library_smoke.py` | Hardware smoke test of the library: start, speeds, clamping, pause/resume, end. **Moves the belt.** |

Captures are written to `captures/`, which is git-ignored because it holds device addresses.

To capture the KS Fit app yourself:
1. Install Apple's Bluetooth logging profile on an iPhone.
2. Use PacketLogger (Additional Tools for Xcode) → File → New iOS Trace, and keep the phone plugged in.
3. Run `uv run tools/pklg_parse.py capture.pklg --no-td`.

## Not yet known

- How the KS Fit app derives the handshake token.
- The meaning of the time-sync reply, `otherSwitch`, `modeStatus` and `autoStop`.
- Whether other properties (child lock, units) are writable.
- What the unused `5833ff01-…` vendor service is for.

PRs and captures from other R3 owners are welcome.

## Upstream contributions

These are R3 fixes and findings contributed back to other projects:

- [mcdax/walkingpad-controller#8](https://github.com/mcdax/walkingpad-controller/pull/8): `stop()` pauses first, because the R3 rejects STOP while running. Also adds `KS-AP-*` detection.
- [mcdax/walkingpad-controller#9](https://github.com/mcdax/walkingpad-controller/pull/9): Supplement protocol and R3 behaviour, added to their protocol reference.
- [mcdax/hass-walkingpad#6](https://github.com/mcdax/hass-walkingpad/pull/6): Home Assistant Bluetooth discovery for `KS-AP-*`.
- [jrosskopf/padctl#2](https://github.com/jrosskopf/padctl/pull/2): pause sends `08 02` instead of STOP, and the FTMS Machine Status codes are fixed.

[python-pyftms](https://github.com/dudanov/python-pyftms), and so the generic [hassio-ftms](https://github.com/dudanov/hassio-ftms) integration, already parses R3 Treadmill Data correctly, including steps.

## Credits / prior art

- [ph4-walkingpad](https://github.com/ph4r05/ph4-walkingpad), the legacy WiLink protocol.
- [walkingpad-controller](https://pypi.org/project/walkingpad-controller/). Its KS Fit decompilation notes gave the Supplement property ids.
- [padctl](https://github.com/jrosskopf/padctl), a FTMS parser and KS-HD-Z1D capture.
- [qdomyos-zwift](https://github.com/cagnulein/qdomyos-zwift), the R2/X21/G1 cipher protocol.

## License

MIT. Not affiliated with KingSmith. Use at your own risk.
