"""Async BLE client for the KingSmith R3 walking pad (bleak)."""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol as p

log = logging.getLogger(__name__)

# The pad occasionally ignores CCCD writes right after connecting (the KS Fit
# app rewrites them three times). We verify Treadmill Data flows and resubscribe.
_SUBSCRIBE_ATTEMPTS = 4
_TELEMETRY_TIMEOUT = 2.5
_CCCD_GAP = 0.2
# After START the pad runs a ~4 s 3-2-1 countdown at 0 km/h. During it, PAUSE
# and STOP are both refused and the belt starts anyway once it ends, so a halt
# requested then must wait for the belt to move and pause it straight away.
_COUNTDOWN_GRACE = 8.0


class WalkingPadError(Exception):
    pass


class ControlError(WalkingPadError):
    def __init__(self, op: int, result: int):
        self.op, self.result = op, result
        name = p.Result(result).name if result in p.Result._value2member_map_ else f"0x{result:02x}"
        super().__init__(f"control op 0x{op:02x} failed: {name}")


@dataclass(frozen=True)
class SpeedRange:
    min_kmh: float
    max_kmh: float
    step_kmh: float


async def discover(timeout: float = 8.0) -> list[BLEDevice]:
    """Find KingSmith R3 pads (name prefix ``KS-AP-``) in range."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return [d for d, adv in found.values() if (adv.local_name or d.name or "").startswith("KS-AP-")]


class WalkingPad:
    """Connect to and control a KingSmith R3.

    >>> async with WalkingPad() as pad:
    ...     await pad.start()
    ...     await pad.set_speed(3.5)
    ...     print(pad.data.steps)
    ...     await pad.stop()

    ``supplement=True`` (default) performs the KingSmith handshake and reads
    properties. Note: after the handshake the pad only streams Treadmill Data
    while the belt is moving (plus a final 0 km/h frame); without it, data
    arrives at 1 Hz even when idle. Use ``supplement=False`` for a pure-FTMS,
    always-streaming connection (no properties / speed-limit info).

    Safety: leaving the ``async with`` block pauses the belt if it is still
    moving or about to start (``pause_on_exit=True``). The pad has no
    application-level watchdog — if your program dies without pausing, the
    belt keeps running. A START cannot be cancelled during its 3-2-1
    countdown; ``pause()`` then waits for the belt to move and pauses it.
    """

    def __init__(
        self,
        device: BLEDevice | str | None = None,
        *,
        name: str = p.DEFAULT_NAME,
        token: bytes = p.KNOWN_TOKENS[0],
        pause_on_exit: bool = True,
        supplement: bool = True,
        scan_timeout: float = 15.0,
    ):
        self._target = device
        self._name = name
        self._token = token
        self.pause_on_exit = pause_on_exit
        self.use_supplement = supplement
        self._scan_timeout = scan_timeout
        self._client: BleakClient | None = None

        self.data: p.TreadmillData = p.TreadmillData()
        self.data_time: float = 0.0
        self.training: p.TrainingStatus | None = None
        self.last_event: p.MachineStatus | None = None
        self.last_record: p.SessionRecord | None = None
        self.properties = p.Properties()
        self.speed_range: SpeedRange | None = None
        self.supplement_ready = False

        self.on_data: Callable[[p.TreadmillData], None] | None = None
        self.on_machine_status: Callable[[p.MachineStatus], None] | None = None
        self.on_training_status: Callable[[p.TrainingStatus], None] | None = None
        self.on_session_record: Callable[[p.SessionRecord], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None

        self._cp_waiters: dict[int, asyncio.Future] = {}
        self._supp_waiters: list[tuple[Callable[[p.SuppFrame], bool], asyncio.Future]] = []
        self._td_event = asyncio.Event()
        self._start_sent_at: float | None = None

    # ------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> WalkingPad:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self.pause_on_exit and self.connected and (self.data.moving or self.starting):
                log.info("pausing belt on exit")
                await self.pause()
        finally:
            await self.disconnect()

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def connect(self) -> None:
        target = self._target
        if target is None:
            target = await BleakScanner.find_device_by_name(self._name, timeout=self._scan_timeout)
            if target is None:
                raise WalkingPadError(
                    f"{self._name} not found. Is it powered on and not connected to another app (KS Fit)?"
                )
        self._client = BleakClient(target, timeout=20, disconnected_callback=self._on_disconnected)
        await self._client.connect()

        mn, mx, inc = struct.unpack("<HHH", bytes(await self._client.read_gatt_char(p.SPEED_RANGE)))
        self.speed_range = SpeedRange(mn / 100, mx / 100, inc / 100)

        await self._subscribe()
        if not self.use_supplement:
            return
        try:
            await self.handshake()
            await self.refresh_properties()
        except asyncio.TimeoutError:
            log.warning("Supplement channel did not respond; properties unavailable (token rejected?)")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()

    def _on_disconnected(self, _client) -> None:
        log.info("disconnected")
        for fut in list(self._cp_waiters.values()) + [f for _, f in self._supp_waiters]:
            if not fut.done():
                fut.set_exception(WalkingPadError("disconnected"))
        if self.on_disconnect:
            self.on_disconnect()

    async def _subscribe(self) -> None:
        assert self._client
        subs = [
            (p.TREADMILL_DATA, self._handle_td),
            (p.TRAINING_STATUS, self._handle_ts),
            (p.CONTROL_POINT, self._handle_cp),
            (p.MACHINE_STATUS, self._handle_fms),
            (p.SUPPLEMENT_NOTIFY, self._handle_supp),
        ]
        for attempt in range(1, _SUBSCRIBE_ATTEMPTS + 1):
            self._td_event.clear()
            for uuid, handler in subs:
                if attempt > 1:
                    try:
                        await self._client.stop_notify(uuid)
                    except Exception:  # noqa: BLE001
                        pass
                await self._client.start_notify(uuid, _safe(handler))
                await asyncio.sleep(_CCCD_GAP)
            try:
                await asyncio.wait_for(self._td_event.wait(), _TELEMETRY_TIMEOUT)
                log.debug("telemetry flowing after attempt %d", attempt)
                return
            except asyncio.TimeoutError:
                log.warning("no Treadmill Data after subscribe attempt %d; retrying", attempt)
        raise WalkingPadError("telemetry never started after resubscribing")

    # --------------------------------------------------------- notifications

    def _handle_td(self, _s, d: bytearray) -> None:
        self.data = p.parse_treadmill_data(bytes(d))
        self.data_time = time.monotonic()
        if self.data.moving:
            self._start_sent_at = None
        self._td_event.set()
        if self.on_data:
            self.on_data(self.data)

    def _handle_ts(self, _s, d: bytearray) -> None:
        self.training = p.parse_training_status(bytes(d))
        if self.on_training_status:
            self.on_training_status(self.training)

    def _handle_fms(self, _s, d: bytearray) -> None:
        self.last_event = p.parse_machine_status(bytes(d))
        if self.on_machine_status:
            self.on_machine_status(self.last_event)

    def _handle_cp(self, _s, d: bytearray) -> None:
        r = p.parse_control_response(bytes(d))
        if r and (fut := self._cp_waiters.pop(r.request, None)) and not fut.done():
            fut.set_result(r)

    def _handle_supp(self, _s, d: bytearray) -> None:
        f = p.parse_supp_frame(bytes(d))
        if f.cmd == p.SuppCmd.RECORD and f.flags == p.SuppFlag.PUSH:
            self.last_record = p.parse_session_record(f.payload)
            if self.on_session_record:
                self.on_session_record(self.last_record)
        for pred, fut in list(self._supp_waiters):
            if not fut.done() and pred(f):
                self._supp_waiters.remove((pred, fut))
                fut.set_result(f)

    # --------------------------------------------------------------- control

    async def _cp(self, payload: bytes, timeout: float = 3.0) -> p.ControlResponse:
        assert self._client
        op = payload[0]
        fut = asyncio.get_running_loop().create_future()
        self._cp_waiters[op] = fut
        await self._client.write_gatt_char(p.CONTROL_POINT, payload, response=True)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._cp_waiters.pop(op, None)

    async def _control(
        self, payload: bytes, verify: Callable[[], bool] | None = None, verify_timeout: float = 6.0
    ) -> p.ControlResponse | None:
        """Request control, then send — the KS Fit app does this before every command.

        The pad occasionally drops a Control Point indication even though the
        command took effect. If ``verify`` is given, a missing indication is
        tolerated when ``verify()`` becomes true from live data; returns None then.
        """
        try:
            rc = await self._cp(p.cmd_request_control())
            if not rc.ok:
                raise ControlError(p.Op.REQUEST_CONTROL, rc.result)
        except asyncio.TimeoutError:
            log.warning("no indication for REQUEST_CONTROL; continuing")
        try:
            r = await self._cp(payload)
        except asyncio.TimeoutError:
            if verify and await self.wait_for(verify, verify_timeout):
                log.warning("no indication for op 0x%02x, but live data confirms it took effect", payload[0])
                return None
            raise WalkingPadError(f"no response to op 0x{payload[0]:02x} and no observed effect") from None
        if not r.ok:
            raise ControlError(payload[0], r.result)
        return r

    async def wait_for(self, pred: Callable[[], bool], timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            await asyncio.sleep(0.2)
        return pred()

    @property
    def starting(self) -> bool:
        """True while a start is under way but the belt isn't moving yet (countdown).

        Covers our own START and starts from the remote or another app (seen
        via Training Status).
        """
        if self.data.moving:
            return False
        if self.training and self.training.state in (p.TrainingState.COUNTDOWN, p.TrainingState.RUNNING):
            return True
        return self._start_sent_at is not None and time.monotonic() - self._start_sent_at < _COUNTDOWN_GRACE

    async def start(self, wait: bool = True, timeout: float = 20.0) -> None:
        """Start or resume. With ``wait``, returns once the belt reports speed > 0 (~5 s incl. countdown)."""
        moving = lambda: self.data.moving  # noqa: E731
        self._start_sent_at = time.monotonic()
        await self._control(p.cmd_start(), verify=moving, verify_timeout=timeout)
        if wait and not await self.wait_for(moving, timeout):
            raise WalkingPadError("belt did not start")

    async def set_speed(self, kmh: float, settle: float = 2.0) -> float:
        """Set the target speed, clamped to the pad's current limit. Starts the belt if needed.

        Returns the speed actually requested.
        """
        lo = self.speed_range.min_kmh if self.speed_range else 1.0
        hi = self.max_speed_kmh
        kmh = round(min(max(kmh, lo), hi), 1)
        if not self.data.moving:
            await self.start()
            await asyncio.sleep(settle)  # let the motor settle before the first speed change

        def applied() -> bool:
            ev = self.last_event
            return (ev is not None and ev.target_speed_kmh == kmh) or abs(self.data.speed_kmh - kmh) < 0.05

        await self._control(p.cmd_set_speed(kmh), verify=applied)
        return kmh

    async def pause(self, wait: bool = True, timeout: float = 15.0) -> None:
        """Pause (belt ramps down, session kept). The safe way to halt the belt.

        The pad refuses PAUSE when the belt is already at rest (idle or
        paused); that is not an error. It also refuses PAUSE during the
        3-2-1 start countdown and then starts the belt anyway: in that case
        this waits for the belt to move and pauses it straight away.
        """
        stopped = lambda: not self.data.moving  # noqa: E731
        try:
            await self._control(p.cmd_pause(), verify=stopped, verify_timeout=timeout)
        except ControlError:
            if self.data.moving:
                raise
            if not self.starting:
                log.debug("PAUSE refused at rest; nothing to pause")
                return
            log.warning("PAUSE refused during start countdown; pausing as soon as the belt moves")
            if not await self.wait_for(lambda: self.data.moving, _COUNTDOWN_GRACE):
                if self.starting:
                    raise WalkingPadError("start countdown did not finish; belt state unknown") from None
                return
            await self._control(p.cmd_pause(), verify=stopped, verify_timeout=timeout)
        if wait and not await self.wait_for(stopped, timeout):
            raise WalkingPadError("belt did not stop after pause")

    halt = pause

    async def stop(self) -> None:
        """End the session (pausing first, including during a start countdown). Counters reset to zero."""
        await self.pause()
        await self._control(
            p.cmd_stop(), verify=lambda: self.data.elapsed_s == 0 and not self.data.moving
        )

    @property
    def max_speed_kmh(self) -> float:
        """Current speed limit: 12.0 with the handle up, 6.0 with it down."""
        limit = self.properties.max_speed_kmh
        rng = self.speed_range.max_kmh if self.speed_range else 12.0
        return min(limit, rng) if limit else rng

    # ------------------------------------------------------------ supplement

    async def _supp(self, frame: bytes, pred: Callable[[p.SuppFrame], bool], timeout: float = 2.0) -> p.SuppFrame:
        assert self._client
        fut = asyncio.get_running_loop().create_future()
        entry = (pred, fut)
        self._supp_waiters.append(entry)
        await self._client.write_gatt_char(p.SUPPLEMENT_WRITE, frame, response=True)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            if entry in self._supp_waiters:
                self._supp_waiters.remove(entry)

    async def handshake(self, token: bytes | None = None) -> None:
        """Unlock the Supplement channel. Until this succeeds the pad ignores every Supplement frame."""
        await self._supp(
            p.cmd_handshake(token or self._token),
            lambda f: f.cmd == p.SuppCmd.SESSION and f.flags == p.SuppFlag.RESPONSE,
        )
        self.supplement_ready = True

    async def refresh_properties(self) -> p.Properties:
        f = await self._supp(
            p.GET_PROPERTIES, lambda f: f.cmd == p.SuppCmd.PROPERTY and f.flags == p.SuppFlag.RESPONSE
        )
        self.properties = p.Properties(p.parse_properties(f.payload))
        return self.properties

    async def sync_clock(self) -> bytes:
        """Set the pad's clock to now (as the KS Fit app does on connect). Returns the raw reply payload."""
        f = await self._supp(
            p.cmd_time_sync(), lambda f: f.cmd == p.SuppCmd.SESSION and f.flags == p.SuppFlag.SET_RESPONSE
        )
        return f.payload


def _safe(fn):
    """bleak swallows exceptions raised in notification callbacks; log them instead."""

    def wrapper(sender, data):
        try:
            fn(sender, data)
        except Exception:  # noqa: BLE001
            log.exception("error handling notification %s", bytes(data).hex())

    return wrapper
