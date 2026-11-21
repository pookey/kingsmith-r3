"""WalkingPad pause/stop against a scripted fake pad (no BLE).

Behaviour mirrored from the real R3 (2026-11-20):
- PAUSE at rest (idle or already paused) -> 80 08 04 02, nothing happens.
- During the 3-2-1 start countdown, PAUSE and STOP are refused and the belt
  starts at 1.0 km/h once the countdown ends.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from kingsmith_r3 import client as c
from kingsmith_r3 import protocol as p

PAUSE, STOP, START = p.cmd_pause(), p.cmd_stop(), p.cmd_start()
GAP = 0.05


def td(kmh: float, elapsed: int = 10) -> bytes:
    # flags 0x0400: speed + elapsed time
    return struct.pack("<HHH", 0x0400, round(kmh * 100), elapsed)


def ts(state: int, digit: str = "") -> bytes:
    return bytes([0x03 if digit else 0x01, state]) + (digit.encode() + b"\0\0\0" if digit else b"")


class FakePad:
    """Feeds notifications into a WalkingPad and answers Control Point writes."""

    def __init__(self, *, state: str) -> None:
        self.pad = c.WalkingPad("fake", supplement=False)
        self.state = state  # "idle" | "paused" | "running" | "countdown"
        self.sent: list[bytes] = []
        self.speed_at_send: list[float] = []
        self.tasks: list[asyncio.Task] = []
        self.pad._cp = self._cp  # type: ignore[method-assign]
        speed = 2.0 if state == "running" else 0.0
        self.pad._handle_td(0, bytearray(td(speed)))
        if state == "countdown":
            self.pad._handle_ts(0, bytearray(ts(p.TrainingState.COUNTDOWN, "2")))

    def later(self, coro) -> None:
        self.tasks.append(asyncio.create_task(coro))

    async def _frames(self, *speeds: float, elapsed: int = 10) -> None:
        for s in speeds:
            await asyncio.sleep(GAP)
            self.pad._handle_td(0, bytearray(td(s, elapsed)))

    async def _countdown_then_run(self) -> None:
        await asyncio.sleep(GAP * 3)
        self.pad._handle_ts(0, bytearray(ts(p.TrainingState.RUNNING)))
        self.state = "running"
        await self._frames(1.0, 1.0)

    async def _cp(self, payload: bytes, timeout: float = 3.0) -> p.ControlResponse:
        self.sent.append(payload)
        self.speed_at_send.append(self.pad.data.speed_kmh)
        op = payload[0]
        ok = p.Result.SUCCESS
        refused = p.Result.OPERATION_FAILED
        if op == p.Op.REQUEST_CONTROL:
            return p.ControlResponse(op, ok)
        if payload == PAUSE:
            if self.state == "running":
                self.state = "paused"
                self.pad._handle_ts(0, bytearray(ts(p.TrainingState.PAUSED)))
                self.later(self._frames(1.0, 0.0))
                return p.ControlResponse(op, ok)
            if self.state == "countdown" and not self.tasks:
                self.later(self._countdown_then_run())
            return p.ControlResponse(op, refused)
        if payload == STOP:
            if self.state == "paused":
                self.state = "idle"
                self.pad._handle_ts(0, bytearray(ts(p.TrainingState.IDLE)))
                self.later(self._frames(0.0, elapsed=0))
                return p.ControlResponse(op, ok)
            return p.ControlResponse(op, refused)
        if payload == START:
            self.state = "countdown"
            self.later(self._countdown_then_run())
            return p.ControlResponse(op, ok)
        return p.ControlResponse(op, ok)

    def run(self, coro_fn):
        async def go():
            try:
                return await coro_fn(self.pad)
            finally:
                for t in self.tasks:
                    t.cancel()

        return asyncio.run(go())

    @property
    def commands(self) -> list[bytes]:
        return [b for b in self.sent if b[0] != p.Op.REQUEST_CONTROL]


@pytest.fixture(autouse=True)
def short_grace(monkeypatch):
    monkeypatch.setattr(c, "_COUNTDOWN_GRACE", 1.0)


def test_pause_while_running() -> None:
    f = FakePad(state="running")
    f.run(lambda pad: pad.pause())
    assert f.commands == [PAUSE]
    assert not f.pad.data.moving


@pytest.mark.parametrize("state", ["idle", "paused"])
def test_pause_at_rest_is_a_no_op(state: str) -> None:
    f = FakePad(state=state)
    f.run(lambda pad: pad.pause())
    assert f.commands == [PAUSE]


def test_pause_during_countdown_pauses_once_belt_moves() -> None:
    f = FakePad(state="countdown")
    assert f.pad.starting
    f.run(lambda pad: pad.pause())
    assert f.commands == [PAUSE, PAUSE]
    assert f.speed_at_send[-1] == 1.0  # second PAUSE only once the belt moved
    assert not f.pad.data.moving


def test_stop_while_running_pauses_then_stops() -> None:
    f = FakePad(state="running")
    f.run(lambda pad: pad.stop())
    assert f.commands == [PAUSE, STOP]
    assert f.speed_at_send[-1] == 0.0


def test_stop_with_paused_session() -> None:
    f = FakePad(state="paused")
    f.run(lambda pad: pad.stop())
    assert f.commands == [PAUSE, STOP]


def test_stop_during_countdown() -> None:
    f = FakePad(state="countdown")
    f.run(lambda pad: pad.stop())
    assert f.commands == [PAUSE, PAUSE, STOP]
    assert f.speed_at_send[-1] == 0.0
    assert f.state == "idle"  # session ended


def test_stop_with_no_session_raises() -> None:
    f = FakePad(state="idle")
    with pytest.raises(c.ControlError):
        f.run(lambda pad: pad.stop())
    assert f.commands == [PAUSE, STOP]


def test_own_start_counts_as_starting_before_training_status_arrives() -> None:
    f = FakePad(state="idle")

    async def go(pad):
        task = asyncio.create_task(pad.start())
        await asyncio.sleep(0)  # START sent, countdown under way
        assert pad.starting
        await pad.pause()
        await task

    f.run(go)
    assert f.commands == [START, PAUSE, PAUSE]
    assert not f.pad.data.moving and not f.pad.starting


def test_exit_during_countdown_pauses(monkeypatch) -> None:
    f = FakePad(state="countdown")

    async def noop():
        return None

    f.pad._client = type("C", (), {"is_connected": True})()  # type: ignore[assignment]
    monkeypatch.setattr(f.pad, "disconnect", noop)
    f.run(lambda pad: pad.__aexit__(None, None, None))
    assert f.commands == [PAUSE, PAUSE]
    assert not f.pad.data.moving
