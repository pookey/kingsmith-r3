"""Pure protocol code for the KingSmith R3 (BLE name ``KS-AP-RQ3``, model ``KS-R3Q``).

No Bluetooth here — only byte building and parsing, so it is easy to test and
to port. See ``docs/PROTOCOL.md`` for how each piece was established.

The pad speaks two things over BLE GATT:

* Standard **FTMS** (Fitness Machine Service, 0x1826): live data, start /
  speed / pause / stop, status events.
* KingSmith's **Supplement** channel (service ``24e2521c-…-c5330a00fdf7``): a
  small framed protocol for a handshake, clock sync, device properties
  (speed limit, units, child lock, …) and session records.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum

# --------------------------------------------------------------------------- UUIDs


def _u16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


FTMS_SERVICE = _u16(0x1826)
FEATURE = _u16(0x2ACC)
TREADMILL_DATA = _u16(0x2ACD)
TRAINING_STATUS = _u16(0x2AD3)
SPEED_RANGE = _u16(0x2AD4)
CONTROL_POINT = _u16(0x2AD9)
MACHINE_STATUS = _u16(0x2ADA)

SUPPLEMENT_SERVICE = "24e2521c-f63b-48ed-85be-c5330a00fdf7"
SUPPLEMENT_NOTIFY = "24e2521c-f63b-48ed-85be-c5330b00fdf7"
SUPPLEMENT_WRITE = "24e2521c-f63b-48ed-85be-c5330d00fdf7"

# Present on the R3 but never used by the KS Fit app; purpose unknown.
VENDOR_5833_SERVICE = "5833ff01-9b8b-5191-6142-22a4536ef123"

DEFAULT_NAME = "KS-AP-RQ3"

# ------------------------------------------------------------------- FTMS control


class Op(IntEnum):
    """FTMS Control Point opcodes used by this pad."""

    REQUEST_CONTROL = 0x00
    RESET = 0x01
    SET_TARGET_SPEED = 0x02
    START_OR_RESUME = 0x07
    STOP_OR_PAUSE = 0x08
    RESPONSE = 0x80


class Result(IntEnum):
    SUCCESS = 0x01
    NOT_SUPPORTED = 0x02
    INVALID_PARAMETER = 0x03
    OPERATION_FAILED = 0x04
    CONTROL_NOT_PERMITTED = 0x05


STOP = 0x01
PAUSE = 0x02


def cmd_request_control() -> bytes:
    return bytes([Op.REQUEST_CONTROL])


def cmd_start() -> bytes:
    """Start or resume. The pad counts down 3-2-1 and runs at its minimum speed (1.0 km/h)."""
    return bytes([Op.START_OR_RESUME])


def cmd_set_speed(kmh: float) -> bytes:
    """Set target speed in km/h (0.01 resolution on the wire, pad rounds to 0.1)."""
    return bytes([Op.SET_TARGET_SPEED]) + struct.pack("<H", round(kmh * 100))


def cmd_pause() -> bytes:
    """Pause. Belt ramps down; session counters are kept. This is what the remote's stop button does."""
    return bytes([Op.STOP_OR_PAUSE, PAUSE])


def cmd_stop() -> bytes:
    """End the session. **Only accepted while paused** — returns OPERATION_FAILED while the belt runs."""
    return bytes([Op.STOP_OR_PAUSE, STOP])


@dataclass(frozen=True)
class ControlResponse:
    request: int
    result: int
    params: bytes = b""

    @property
    def ok(self) -> bool:
        return self.result == Result.SUCCESS


def parse_control_response(b: bytes) -> ControlResponse | None:
    if len(b) >= 3 and b[0] == Op.RESPONSE:
        return ControlResponse(b[1], b[2], bytes(b[3:]))
    return None


# --------------------------------------------------------------- FTMS live data


@dataclass
class TreadmillData:
    """One FTMS Treadmill Data notification (~1 Hz).

    Distance, kcal and steps only accumulate while the foot sensor detects
    walking; elapsed time runs whenever the belt runs.
    """

    speed_kmh: float = 0.0
    distance_m: int = 0
    kcal: int = 0
    kcal_per_hour: int = 0
    kcal_per_min: int = 0
    elapsed_s: int = 0
    steps: int = 0
    flags: int = 0
    raw: bytes = b""

    @property
    def moving(self) -> bool:
        return self.speed_kmh > 0


def parse_treadmill_data(b: bytes) -> TreadmillData:
    """Parse FTMS Treadmill Data, including KingSmith's bit-13 step count.

    The R3 always sends flags 0x2484: speed, total distance, expended energy,
    elapsed time, and the KingSmith step extension (uint24 LE).
    """
    if len(b) < 2:
        raise ValueError("Treadmill Data too short")
    flags = struct.unpack_from("<H", b)[0]
    o = 2
    td = TreadmillData(flags=flags, raw=bytes(b))

    def need(n: int) -> None:
        if o + n > len(b):
            raise ValueError(f"Treadmill Data truncated (flags=0x{flags:04x}, len={len(b)})")

    if not flags & 0x0001:
        need(2)
        td.speed_kmh = struct.unpack_from("<H", b, o)[0] / 100
        o += 2
    if flags & 0x0002:  # average speed
        o += 2
    if flags & 0x0004:
        need(3)
        td.distance_m = b[o] | b[o + 1] << 8 | b[o + 2] << 16
        o += 3
    if flags & 0x0008:  # inclination + ramp angle
        o += 4
    if flags & 0x0010:  # elevation gain
        o += 4
    if flags & 0x0020:  # instantaneous pace
        o += 1
    if flags & 0x0040:  # average pace
        o += 1
    if flags & 0x0080:
        need(5)
        td.kcal, td.kcal_per_hour, td.kcal_per_min = struct.unpack_from("<HHB", b, o)
        o += 5
    if flags & 0x0100:  # heart rate
        o += 1
    if flags & 0x0200:  # METs
        o += 1
    if flags & 0x0400:
        need(2)
        td.elapsed_s = struct.unpack_from("<H", b, o)[0]
        o += 2
    if flags & 0x0800:  # remaining time
        o += 2
    if flags & 0x1000:  # force on belt + power
        o += 4
    if flags & 0x2000:  # KingSmith: step count
        need(3)
        td.steps = b[o] | b[o + 1] << 8 | b[o + 2] << 16
        o += 3
    return td


class MachineEvent(IntEnum):
    """FTMS Fitness Machine Status event opcodes."""

    RESET = 0x01
    STOPPED_OR_PAUSED = 0x02
    STOPPED_BY_SAFETY_KEY = 0x03
    STARTED_OR_RESUMED = 0x04
    TARGET_SPEED_CHANGED = 0x05
    CONTROL_LOST = 0xFF


@dataclass(frozen=True)
class MachineStatus:
    event: int
    target_speed_kmh: float | None = None  # for TARGET_SPEED_CHANGED (e.g. remote +/-)
    stopped: bool | None = None  # for STOPPED_OR_PAUSED: True = stop, False = pause
    raw: bytes = b""


def parse_machine_status(b: bytes) -> MachineStatus:
    if not b:
        raise ValueError("empty Machine Status")
    ev = b[0]
    if ev == MachineEvent.TARGET_SPEED_CHANGED and len(b) >= 3:
        return MachineStatus(ev, target_speed_kmh=struct.unpack_from("<H", b, 1)[0] / 100, raw=bytes(b))
    if ev == MachineEvent.STOPPED_OR_PAUSED and len(b) >= 2:
        return MachineStatus(ev, stopped=b[1] == STOP, raw=bytes(b))
    return MachineStatus(ev, raw=bytes(b))


class TrainingState(IntEnum):
    """Training Status codes as this pad uses them (vendor meanings, not the FTMS spec's)."""

    IDLE = 0x01
    RUNNING = 0x0D
    COUNTDOWN = 0x0E
    PAUSED = 0x0F


@dataclass(frozen=True)
class TrainingStatus:
    state: int
    text: str = ""  # countdown digit ("3", "2", "1", "0") during COUNTDOWN
    raw: bytes = b""


def parse_training_status(b: bytes) -> TrainingStatus:
    if len(b) < 2:
        raise ValueError("Training Status too short")
    text = ""
    if b[0] & 0x01 and len(b) > 2:
        text = bytes(b[2:]).split(b"\x00", 1)[0].decode("ascii", "replace")
    return TrainingStatus(b[1], text, bytes(b))


# ------------------------------------------------------------------ Supplement

# A handshake token captured from the KS Fit app. The pad validates tokens (any
# altered byte is ignored) but does not tie them to a connection or expire them.
# Whether it works on *other* R3 units is unknown — please report!
KNOWN_TOKENS = (bytes.fromhex("87b4525133"), bytes.fromhex("fa27535133"))

GET_PROPERTIES = bytes.fromhex("7200000072")  # verbatim from KS Fit (extra 0x00 not counted by len)


class SuppFlag(IntEnum):
    REQUEST = 0x00
    SET = 0x01
    PUSH = 0x50
    RESPONSE = 0x80
    SET_RESPONSE = 0x81


class SuppCmd(IntEnum):
    SESSION = 0x71  # handshake (flags 00) / time sync (flags 01)
    PROPERTY = 0x72  # get list (00) / set (01)
    RECORD = 0x73  # session record push (flags 50)


class Prop(IntEnum):
    """Property ids (names from the KS Fit app's FTMSPropertyType enum)."""

    UNIT = 1
    AUTO_STOP = 2
    REAL_TIME_PACE = 3
    MOTOR_VERSION = 4
    ERR_CODE = 5
    CHILD_LOCK = 6
    MAX_LIMIT_SPEED = 7
    OTHER_SWITCH = 8
    READ_ONLY = 9
    MODE_STATUS = 10
    MAX_SPEED = 11
    ZONE_TIME = 12
    LIGHT_COLOR = 13


READ_ONLY_HANDLE_UP = 0x08  # bit in Prop.READ_ONLY: speed handle raised (limit 12.0 instead of 6.0)


def supp_frame(cmd: int, flags: int, payload: bytes = b"") -> bytes:
    """Build ``[cmd][flags][len][payload][sum & 0xFF]``."""
    body = bytes([cmd, flags, len(payload)]) + payload
    return body + bytes([sum(body) & 0xFF])


def cmd_handshake(token: bytes = KNOWN_TOKENS[0]) -> bytes:
    if len(token) != 5:
        raise ValueError("token must be 5 bytes")
    return supp_frame(SuppCmd.SESSION, SuppFlag.REQUEST, token)


def cmd_time_sync(when: datetime | None = None) -> bytes:
    ts = int((when or datetime.now(timezone.utc)).timestamp())
    return supp_frame(SuppCmd.SESSION, SuppFlag.SET, struct.pack("<I", ts) + bytes([1, 0, 0, 0]))


def cmd_set_property(prop: int, value: int) -> bytes:
    """Set a property (KS Fit format). Note: MAX_LIMIT_SPEED is not writable — it follows the handle."""
    return supp_frame(SuppCmd.PROPERTY, SuppFlag.SET, bytes([prop]) + struct.pack("<H", value))


@dataclass(frozen=True)
class SuppFrame:
    cmd: int
    flags: int
    payload: bytes
    checksum_ok: bool
    raw: bytes = b""


def parse_supp_frame(b: bytes) -> SuppFrame:
    if len(b) < 4:
        raise ValueError("Supplement frame too short")
    n = b[2]
    payload = bytes(b[3 : 3 + n])
    chk = b[-1]
    # 0x71/0x72 frames: sum of all bytes. 0x73 push frames: sum excluding byte 0. Accept either.
    ok = len(b) == n + 4 and chk in (sum(b[:-1]) & 0xFF, sum(b[1:-1]) & 0xFF)
    return SuppFrame(b[0], b[1], payload, ok, bytes(b))


def parse_properties(payload: bytes) -> dict[int, int]:
    """Property-list payload: repeated (uint16 LE id, uint16 LE value)."""
    if len(payload) % 4:
        raise ValueError("property payload length not a multiple of 4")
    return {pid: val for pid, val in (struct.unpack_from("<HH", payload, i) for i in range(0, len(payload), 4))}


@dataclass(frozen=True)
class SessionRecord:
    """Pushed by the pad on pause, on end, and at connect."""

    start: datetime | None
    end: datetime | None  # None while the session is still open
    unknown: int = 1


def parse_session_record(payload: bytes) -> SessionRecord:
    if len(payload) < 13:
        raise ValueError("session record too short")
    start, unk, end = struct.unpack_from("<III", payload, 1)

    def dt(v: int) -> datetime | None:
        return datetime.fromtimestamp(v, timezone.utc) if v else None

    return SessionRecord(dt(start), dt(end), unk)


@dataclass
class Properties:
    values: dict[int, int] = field(default_factory=dict)

    def get(self, prop: Prop, default: int | None = None) -> int | None:
        return self.values.get(int(prop), default)

    @property
    def max_speed_kmh(self) -> float | None:
        v = self.get(Prop.MAX_LIMIT_SPEED)
        return v / 10 if v is not None else None

    @property
    def handle_up(self) -> bool | None:
        v = self.get(Prop.READ_ONLY)
        return bool(v & READ_ONLY_HANDLE_UP) if v is not None else None

    @property
    def miles(self) -> bool | None:
        v = self.get(Prop.UNIT)
        return bool(v) if v is not None else None

    @property
    def child_lock(self) -> bool | None:
        v = self.get(Prop.CHILD_LOCK)
        return bool(v) if v is not None else None

    def named(self) -> dict[str, int]:
        return {(Prop(k).name.lower() if k in Prop._value2member_map_ else f"prop_{k}"): v for k, v in self.values.items()}
