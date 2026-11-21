# KingSmith R3 BLE protocol

Everything here was observed on a real pad, from three sources:

- direct probing from a Mac with bleak;
- passive logging;
- Apple PacketLogger captures of the official **KS Fit** iOS app.

The status markers mean:
- ✅ confirmed on the wire.
- 🟡 inferred.
- ❓ unknown.

## 1. Identity & advertising

| | |
|---|---|
| Local name | `KS-AP-RQ3` |
| 2A24 Model | `KS-R3Q` |
| 2A29 Manufacturer | `3Q` |
| 2A26 Firmware | `V74.02.23` |
| 2A28 Software | `V0.0.5` |
| 2A27 Hardware | `ST` |
| 2A25 Serial / 2A23 System ID | device MAC (hex) |
| Advertised service | `0x1826` (FTMS) |
| Service data (0x1826) | `01 00 01`: FTMS available, machine type = treadmill |
| Manufacturer data | company `0x5054`, payload = last 4 bytes of the MAC |
| Pairing / bonding | none. A plain GATT connection works. |
| MTU | 247 (iOS asks for 527) |

- Only **one central** can be connected at a time.
- While connected, the pad stops advertising.

## 2. GATT map

ATT handles are as seen on the wire. bleak on macOS reports value handle − 1.

| Handle | UUID | Props | Purpose |
|---|---|---|---|
| | `180A` Device Information | read | see above |
| 0x1d | `2ACC` Fitness Machine Feature | read | `44 12 00 00 01 00 00 00`: total distance, step count, energy, elapsed time. Target setting: speed only. |
| 0x20 | `2ACD` Treadmill Data | notify | ~1 Hz, both idle and running |
| 0x24 | `2AD3` Training Status | read/notify | |
| 0x28 | `2AD4` Supported Speed Range | read | `64 00 b0 04 0a 00`: 1.00–12.00 km/h, step 0.10 |
| 0x2b | `2AD5` Supported Inclination Range | read | all zero (no incline) |
| 0x2e | `2AD7` Supported Heart Rate Range | read | zero |
| 0x31 | `2AD9` Fitness Machine Control Point | write/indicate | commands |
| 0x35 | `2ADA` Fitness Machine Status | read/notify | events |
| 0x3a | `24e2521c-f63b-48ed-85be-c5330b00fdf7` | notify | KingSmith **Supplement** RX |
| 0x3d | `24e2521c-f63b-48ed-85be-c5330d00fdf7` | write | KingSmith **Supplement** TX |
| | `5833ff02-9b8b-5191-6142-22a4536ef123` | write | ❓ vendor service `5833ff01-…`, never used by KS Fit |
| | `5833ff03-9b8b-5191-6142-22a4536ef123` | notify | ❓ silent |

There's no OTA service (`32e2314c-…`) on this unit.

## 3. Connection setup (what KS Fit does) ✅

```
MTU exchange
CCCD 2A05 Service Changed   = 02 00
read 2ACC, 2AD4
CCCD Treadmill Data         = 01 00
CCCD Training Status        = 01 00
CCCD Control Point          = 02 00 (indicate)
CCCD Machine Status         = 01 00
CCCD Supplement notify      = 01 00
CP   01 (RESET)             -> 80 01 05  (control not permitted; ignored)
SUPP 71 00 05 <token> <chk> -> 71 80 00 f1          handshake
CP   00 (REQUEST_CONTROL)   -> 80 00 01
SUPP 71 01 08 <time> ...    -> 71 81 09 ...         clock sync
SUPP 72 00 00 00 72         -> 72 80 24 <props>     property list
(CCCDs rewritten 3x)
```

⚠️ **The pad sometimes ignores CCCD writes after connecting.** No notifications arrive at all; we saw this on 2 of ~6 connections. Check that Treadmill Data (1 Hz) is arriving, and resubscribe if it isn't. That's probably why KS Fit rewrites the CCCDs.

## 4. FTMS control ✅

- The R3 is **spec-compliant**. Every command gets a Control Point indication `80 <op> <result>`.
- ⚠️ **Rarely, the pad drops the indication even though the command took effect.** We saw a STOP with no `80 08 01 01` while the session still ended. Confirm the effect from live data (speed, FM Status, counters) rather than relying only on the indication.
- **REQUEST_CONTROL (`00`) is required on each connection.** Without it, commands return `05`. KS Fit sends it before every command.

| Command | Bytes | Response | Behaviour |
|---|---|---|---|
| Request control | `00` | `80 00 01` | |
| Start / resume | `07` | `80 07 01` | Training Status counts down 3-2-1-0 (~1 s each), then FM Status `04`, and the belt runs at 1.0 km/h. ~5.5 s in total. |
| Set speed | `02 <u16le km/h×100>` | `80 02 01 <echo>` | Takes effect within 1 s. Clamped to the current limit. The pad rounds down to 0.1 (5.95 → 5.9). |
| Pause | `08 02` | `80 08 01 02` | Belt ramps down over ~4–6 s. Elapsed time freezes and the session is kept. Then FM Status `02 02`, Training Status `01 0f`. |
| Stop (end session) | `08 01` | `80 08 01 01` **only when paused** | FM Status `02 01`, Training Status `01 01`, counters reset to 0 |
| Stop while running | `08 01` | `80 08 04 01` OPERATION_FAILED | ⚠️ **the belt keeps running** |
| Pause at rest (idle, or already paused) | `08 02` | `80 08 04 02` | no effect; harmless |
| Stop with no session | `08 01` | `80 08 04 01` | no effect |
| Pause or Stop **during the start countdown** | `08 02` / `08 01` | `80 08 04 xx` | ⚠️ **both are refused, and the belt starts anyway** when the countdown ends |
| Reset | `01` | `80 01 05` before control | not useful |

- ✅ **A START can't be cancelled.** From the moment START is accepted until the belt moves (~4 s: Training Status `03 0e "3".."0"`, then `01 0d`, FM Status `04`, then speed > 0), the pad refuses PAUSE and STOP, and Treadmill Data reads 0 km/h. To halt, wait for the first frame with speed > 0, then PAUSE. That takes about 1 s at 1 km/h. This applies to starts from the remote or app too, so a speed of 0 alone doesn't prove the belt will stay still. Verified 2026-11-20.
- Setting the speed 3 s after the belt starts moving is fine. Setting it to 12.0 directly from 1.0 works, reaching 12.0 in ~3 s.
- No cold-start disconnects were seen, unlike some other KingSmith FTMS models.

## 5. Live data

### Treadmill Data (`2ACD`) ✅

The flags are always `0x2484`:

```
84 24 | 5e 01 | 0a 00 00 | 01 00 00 00 00 | 28 00 | 07 00 00
flags | speed | distance | kcal  /h  /min | elapsed| steps (KingSmith bit 13, u24le)
      | 3.50  | 10 m     | 1     0   0    | 40 s   | 7
```

- Speed is u16 in units of 0.01 km/h.
- Distance is u24 in metres, but it only updates in 10 m steps.
- kcal is a u16 total. kcal/h and kcal/min are always 0.
- **Distance, kcal and steps only accumulate while the foot sensor detects walking.** At 12 km/h with an empty belt, distance stays 0. Elapsed time runs whenever the belt runs.
- Values persist while paused, even across reconnects, until the session is ended.
- ✅ **Cadence depends on the handshake.** Without a Supplement handshake, frames arrive at 1 Hz even when idle. **After a handshake, frames only arrive while the belt is moving**, plus a final 0 km/h frame when it stops; idle gaps of 5–8 s were seen in both the KS Fit capture and our own sessions. So don't use idle Treadmill Data as a link heartbeat after handshaking. Verify the subscriptions *before* the handshake.
- The numbers match the KS Fit app's session summary exactly. It doesn't use the vendor channel for these.

### Fitness Machine Status (`2ADA`) ✅

| Event | Meaning |
|---|---|
| `04` | started/resumed |
| `02 02` | paused (also the **remote's stop button**) |
| `02 01` | stopped (session ended) |
| `05 <u16le>` | target speed changed. Comes from the remote's +/− (0.5 km/h steps, the first press rounds to a 0.5 boundary) or from our own commands. Targets above the limit are reported, but the belt stays at the limit. |

### Training Status (`2AD3`) ✅

These are vendor meanings, not the FTMS spec's:

| Bytes | State |
|---|---|
| `01 01` | idle |
| `03 0e 3x 00 00 00` | countdown; the status string is the ASCII digit "3".."0" |
| `01 0d` | running |
| `01 0f` | paused |

## 6. Supplement channel (KingSmith vendor)

### Frame ✅

```
[cmd] [flags] [len] [payload × len] [chk]
```

- `chk` is the sum of all preceding bytes & 0xFF.
- Exception: in `0x73` push frames, the checksum excludes the first byte.
- Exception: the get-properties request is sent verbatim as `72 00 00 00 72`, with one extra `00` not counted by len.

| flags | meaning |
|---|---|
| `00` | request / command |
| `01` | set |
| `80` | response to `00` |
| `81` | response to `01` |
| `50` | unsolicited push |

### Gating ✅

**The pad silently ignores every Supplement frame until a valid handshake.** The writes are ACKed at the GATT level, but no notification comes back.

### `0x71 00` handshake ✅

`71 00 05 <5-byte token> <chk>` → `71 80 00 f1`

| Token | Result |
|---|---|
| `87 b4 52 51 33`, `fa 27 53 51 33` (from KS Fit, two different connections) | accepted on any later connection |
| `00 00 00 00 00` | ignored |
| one byte altered | ignored |

- The token has an integrity check, but it isn't bound to a connection and doesn't expire.
- ❓ How KS Fit generates it. It isn't Unix time, and XOR with time isn't constant either.
- ❓ Whether tokens are per-device.

### `0x71 01` clock sync ✅

`71 01 08 <u32le unix time> 01 00 00 00 <chk>` → `71 81 09 03 00 00 00 1f 03 00 00 00 <chk>`

❓ Meaning of the reply, and of the `01` byte.

### `0x72 00` get properties ✅

The response payload is repeated `(u16le id, u16le value)` pairs. The ids match the KS Fit app's `FTMSPropertyType` enum.

| id | name | Value seen | Notes |
|---|---|---|---|
| 1 | unit | 0 | 0 = km/h |
| 2 | autoStop | `0x403c` | ❓ low byte 60 (seconds?) |
| 4 | motorVersion | 115 | |
| 5 | errCode | 0 | |
| 6 | childLock | 0 | |
| 7 | maxLimitSpeed | **60 / 120** | km/h × 10. **6.0 with the handle down, 12.0 with it up** |
| 8 | otherSwitch | `0x0f` | ❓ bitfield |
| 9 | readOnly | **5 / 13** | bit `0x08` = **handle up** |
| 10 | modeStatus | `0x0300` / `0x0320` | ❓ |

### `0x72 01` set property 🟡

`72 01 03 <id u8> <value u16le> <chk>` → `72 81 02 <id> <status>`

- For id 7 the reply is always `07 07`, and the value doesn't change. The limit follows the handle and isn't writable.
- KS Fit sends this (value 120) six times and gets the same refusal.
- ❓ Whether other ids are writable.

### `0x73 50` session record push ✅

`73 50 0d 00 <start u32le> 01 00 00 00 <end u32le | 0> <chk>`

- Pushed at connect (the last session), on pause (end = 0) and on session end.
- The times are Unix timestamps, which is why the app syncs the clock.

## 7. Safety notes

- There's no application-level keepalive or watchdog. **A disconnect doesn't stop the belt.**
- Always halt with PAUSE (`08 02`), and confirm that Treadmill Data reaches 0.
- STOP while running is refused.
- PAUSE and STOP during the 3-2-1 start countdown are refused, and the belt starts anyway. Pause it as soon as it moves.
- Don't trust a connection that isn't delivering Treadmill Data: you can't verify the belt's state.
