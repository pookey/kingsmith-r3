# KingSmith WalkingPad — BLE integration research

_Survey of existing KingSmith/WalkingPad BLE projects, done before probing the R3 (2026-11-20). The R3 turned out to be an FTMS + Supplement device: see [PROTOCOL.md](PROTOCOL.md). Kept here as a map of the wider KingSmith protocol landscape._

## TL;DR

- "KingSmith WalkingPad" covers **at least three different BLE protocols**. Which one applies depends on the model. **The first job is to scan and record our pad's BLE name and advertised services.** That tells us which protocol family we're dealing with.
- The protocols are:
  1. **WiLink (legacy)**: service `0xFE00`, notify `0xFE01`, write `0xFE02`. Binary frames `F7 … FD`. Used by A1, A1 Pro, C1, C2, older R1. Best reference: **ph4-walkingpad**.
  2. **FTMS (modern)**: the standard Bluetooth Fitness Machine Service `0x1826`, plus KingSmith vendor extras. Used by `KS-HD-*` (e.g. Z1), `KS-MC21-*` and similar. Best reference: **walkingpad-controller**, which has excellent reverse-engineering docs taken from the KS Fit app.
  3. **"props" text protocol (obfuscated)**: service `0x1234`, write `0xFED7`, notify `0xFED8`. The payload is the text `props <Key> <Value>`, base64-encoded and then passed through a substitution cipher. Used by R2, X21, G1 and K12 Pro. Best reference: **qdomyos-zwift** (`kingsmithr2treadmill`).
- Only **one BLE central at a time** can connect. The KS Fit app must be closed or disconnected while we work.
- For tooling, **Python + bleak** is the path of least resistance on macOS. The best existing code is in Python, and bleak uses CoreBluetooth natively.

## Existing projects

| Project | Lang | Protocols | Notes |
|---|---|---|---|
| [ph4r05/ph4-walkingpad](https://github.com/ph4r05/ph4-walkingpad) | Python (bleak) | WiLink | The original reverse-engineering work. `ph4_walkingpad/pad.py` holds the whole protocol. CLI `ph4-walkingpad-ctl`. |
| [walkingpad-controller](https://pypi.org/project/walkingpad-controller/) (mcdax, v0.5.1) | Python (bleak) | FTMS, WiLink (via ph4), Sperax | Most complete and most current. `docs/ftms-protocol-reference.md` and `docs/ks-fit-reverse-engineering.md` come from decompiling the KS Fit Flutter APK with blutter, cross-checked against btsnoop captures. Backs a Home Assistant integration (hass-walkingpad). |
| [jrosskopf/padctl](https://github.com/jrosskopf/padctl) | Rust (bluez-async) | FTMS (+ WiLink parser) | TUI controller verified on a KS-HD-Z1D. `src/ble/protocol.rs` has a clean FTMS Treadmill Data parser and unit tests that use a **real captured Z1 packet**. |
| [cagnulein/qdomyos-zwift](https://github.com/cagnulein/qdomyos-zwift) | C++/Qt | WiLink (`kingsmithr1protreadmill`), props/cipher (`kingsmithr2treadmill`) | The only source I found for the R2/X21/G1 cipher protocol. Its `bluetooth.cpp` has a large table mapping BLE name prefixes to drivers. |
| [darnfish/walkingpad](https://github.com/darnfish/walkingpad) | TypeScript (noble) | WiLink | Port of ph4 that queues commands 1 s apart. |
| [indiefan/king_smith](https://github.com/indiefan/king_smith) | HA custom component | WiLink | Belt on/off and speed only. |
| [dudanov/hassio-ftms](https://github.com/dudanov/hassio-ftms) | HA (pyftms) | generic FTMS | Might work with FTMS pads, but untested on KingSmith. |
| [peteh/pacekeeper](https://github.com/peteh/pacekeeper) | ESP32 | — | **Not KingSmith.** It targets PitPat/Superun pads. Ignore. |

## Model → protocol (from name prefixes in the projects above)

| BLE name prefix | Protocol |
|---|---|
| `WalkingPad`, `KINGSMITH`, `KS-ST-A1P` (A1 Pro), `KS-BLC` (C2), `KS-F0*`, `KS-H*` (excluding `KS-HD-`), `KS-BLR*`, `KS-SC-BLR2C` | WiLink `0xFE00` |
| `KS-HD-*` (e.g. `KS-HD-Z1D`) | FTMS + "Supplement" vendor service `24e2521c-…` |
| `KS-MC21-*`, `KS-SMC21C-*`, `ZP-ZEALR1-*` | FTMS + "ODM" vendor pre-amble char `d18d2c10-…` |
| `KS-R1AC`, `KS-HC-R1A*`, `KS-X21*`, `KS-HDSC-X21C`, `KS-NACH-X21C`, `KS-NGCH-X21C`, `KS-NACH-MXG`, `KS-NGCH-G1C`, `KS-ST-K12PRO` | props/cipher (`0x1234`) |

The prefix lists overlap and conflict between projects (for example, walkingpad-controller treats `KS-X21*` as WiLink, while qdomyos treats it as props/cipher). **Don't rely on the name alone. Confirm from the advertised and discovered services.**

---

## Protocol 1 — WiLink (legacy)

- Service `0000fe00-…`. Write char **`0xFE02`**, notify char **`0xFE01`**.
- On macOS, ph4 passes a list of service UUIDs to the scanner, because CoreBluetooth needs them for discovery to work reliably on macOS 12+.

**Command frame (app → pad), 6 bytes:**

```
F7  <type>  <cmd>  <val>  <crc>  FD        crc = sum(bytes[1 .. -2]) & 0xFF
```

| Purpose | Bytes (before CRC) |
|---|---|
| Ask current status | `F7 A2 00 00 __ FD` |
| Set speed (val = km/h × 10, 0 = stop) | `F7 A2 01 <spd> __ FD` |
| Switch mode (0 auto, 1 manual, 2 standby) | `F7 A2 02 <mode> __ FD` |
| Start belt | `F7 A2 04 01 __ FD` |
| Unknown (`cmd_162_3_7`) | `F7 A2 03 07 __ FD` |
| Ask last record / history | `F7 A7 AA FF __ FD` (or `… AA 00 …`) |
| Set preference | `F7 A6 <key> <stype> <v BE24> __ FD` |

Pref keys: 1 target (type: 0 none, 1 dist, 2 cal, 3 time), 3 max speed, 4 start speed, 5 smart-start, 6 sensitivity (1 high … 3 low), 7 display bitmask, 8 units (miles), 9 child lock.

ph4 also has a table of 10-byte "profile" payloads (`PAYLOADS_255`), but their purpose is unknown.

> ⚠️ padctl documents a **7-byte** frame (`F7 A2 cmd val FF crc FD`). ph4's code actually puts the CRC where padctl puts `0xFF` and sends 6 bytes. padctl was only verified on FTMS hardware, so trust ph4 here.

**Rate limit:** ph4 waits ≥0.69 s between commands, and darnfish waits 1 s. The app polls status about every 750 ms.

**Current status notification `F8 A2 …` (~20 bytes, multi-byte values are big-endian):**

| Offset | Field |
|---|---|
| 2 | belt state (0 stopped, 1 running, 5 standby, 9 starting; countdown values exist too) |
| 3 | speed, km/h × 10 |
| 4 | mode (0 auto, 1 manual, 2 standby) |
| 5–7 | elapsed time, s (u24) |
| 8–10 | distance, units of 10 m (u24; /100 = km) |
| 11–13 | steps (u24) |
| 14 | app speed (/30?) |
| 16 | last controller/remote button |
| 15, 17 | unknown |

**Last-record notification `F8 A7 …`:** time at 8–10, distance at 11–13, steps at 14–16. Bytes 2–7 are unknown.

## Protocol 2 — FTMS (modern)

Standard service `0x1826`:

| Char | Use |
|---|---|
| `2ACC` Fitness Machine Feature | read: feature flags |
| `2ACD` Treadmill Data | notify, ~10 Hz while moving and ~1 Hz idle |
| `2AD3` Training Status | notify |
| `2AD4` Supported Speed Range | read: min/max/increment in 0.01 km/h |
| `2AD9` Control Point | write + indicate |
| `2ADA` Fitness Machine Status | notify: events, including ones caused by the remote control |

**Control Point opcodes:**

| Opcode | Command |
|---|---|
| `00` | REQUEST_CONTROL (KingSmith often answers `80 00 04` OPERATION_FAILED, which is safe to ignore) |
| `02 <u16le spd×100>` | Set target speed |
| `07` | Start/resume |
| `08 01` / `08 02` | Stop / pause |

Responses arrive as `80 <op> <result>`. Some firmware **never indicates success** and reports changes through `2ADA` instead.

**Treadmill Data** follows the standard flag-driven layout. Bit **13 (0x2000)** is a **KingSmith extension**: a step count appended after the standard fields (u16le + pad byte, or equivalently u24le).

Real Z1 packet from padctl's tests:

```
84 24 | F4 01 | D2 05 00 | 68 00 00 00 00 | 3A 05 | 06 09 00
flags | 5.00  | 1490 m   | 104 kcal …     | 1338 s | 2310 steps
```

**Fitness Machine Status `2ADA` events (standard values):**

| Event | Meaning |
|---|---|
| `01` | reset |
| `02 <1 stop / 2 pause>` | stopped or paused |
| `03` | safety key |
| `04` | started/resumed |
| `05 <u16 spd>` | target speed changed |
| `FF` | control lost |

padctl's mapping of `02` and `03` is swapped relative to the spec. Use the spec.

**KingSmith quirks (from walkingpad-controller):**
- **CCCD writes must be staggered.** KS Fit waits 100/200/300 ms between notification subscriptions, and the firmware silently drops CCCD writes that arrive less than ~30 ms apart.
- **Cold start:** send `START_OR_RESUME`, then wait until Treadmill Data reports speed > 0 and give it a short settle time before `SET_TARGET_SPEED`. Sending speed too early drops the BLE link.
- **MC-21 family:** write the magic `01 00 0d 00 06 0b 0f 0d` to `d18d2c10-c44c-11e8-a355-529269fb1459` before **each** Control Point write. Without it you get CONTROL_NOT_PERMITTED.
- **KS-HD-\* "Supplement" service** `24e2521c-f63b-48ed-85be-c5330a00fdf7`:
  - notify `…0b00fdf7`, write `…0d00fdf7`.
  - v6 app firmware adds `…0e00fdf7` (notify) and `…0f00fdf7` (write).
  - Carries property get/set over 14 property IDs (unit, autoStop, childLock, maxLimitSpeed, lightColor, errCode, …), device unlock, system info, programs and OTA.
  - **Its frame format is largely undecoded. This is the most interesting area for new reverse-engineering.**
- OTA service `32e2314c-…fdf1/fdf2` is present. **Never write to it.**
- No application-level keepalive. The notification stream keeps the link alive.
- After an abrupt disconnect the firmware can refuse connections for a few seconds. walkingpad-controller retries 5× with 3 s gaps.

## Protocol 3 — "props" text over a substitution cipher (R2 / X21 / G1)

- Service `0x1234`, which can also appear as `00011234-…` or `00021234-…`. Write `FED7`, notify `FED8`, with matching `0001…`/`0002…` variants.
- **Encoding:**
  1. The plaintext is ASCII: `props <Key> <Value> [<Key> <Value> …]`.
  2. Base64-encode it.
  3. Map each base64 character through a 65-char substitution table.
  4. Append `\r`.
  5. Write it in 16-byte chunks.
- Decoding is the reverse, buffering chunks until the `\r`. The tables are in `qdomyos-zwift/src/devices/kingsmithr2treadmill/kingsmithr2treadmill.h`. There are **seven known table variants** (v1–v7, including a "G1 walking pad" one), and different devices or firmware use different tables.
- **Known keys:**

| Key | Meaning |
|---|---|
| `CurrentSpeed` | speed |
| `ControlMode` | 0 auto, 1 manual, 2 standby |
| `runState` | 0 stop, 1 start |
| `RunningDistance` | metres |
| `RunningSteps` | steps |
| `BurnCalories` | kcal × 1000 |
| `RunningTotalTime` | seconds |
| `spm` | steps per minute |
| `mcu_version` | firmware version |
| `goal` | goal |
| `Error` | error code |

- **Example commands:** `props CurrentSpeed 3.0`, `props ControlMode 1`, `props runState 1`.
- If our pad uses this protocol and none of the seven tables decodes its traffic, we can recover the table by known-plaintext. Every message starts with `props `, which base64-encodes to `cHJvcHMg`.

---

## macOS practicalities

- CoreBluetooth hides MAC addresses, so bleak reports a per-host **UUID** instead. Store that UUID, or match on the device name.
- The terminal app (Ghostty, iTerm, Terminal, …) needs **Bluetooth permission** under System Settings → Privacy & Security → Bluetooth. Without it, scans silently return nothing or the process crashes.
- The pad must be **powered on and not connected to KS Fit or anything else**. If the phone app auto-connects, kill it or turn off Bluetooth on the phone.
