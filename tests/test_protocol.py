"""Tests use frames captured from a real KingSmith R3 (KS-AP-RQ3, FW V74.02.23)
and from the KS Fit iOS app talking to it."""

from datetime import datetime, timezone

import pytest

from kingsmith_r3 import protocol as p

H = bytes.fromhex


# ------------------------------------------------------------------ FTMS commands


@pytest.mark.parametrize(
    "fn,expected",
    [
        (p.cmd_request_control, "00"),
        (p.cmd_start, "07"),
        (p.cmd_pause, "0802"),
        (p.cmd_stop, "0801"),
    ],
)
def test_simple_commands(fn, expected):
    assert fn() == H(expected)


@pytest.mark.parametrize("kmh,expected", [(1.0, "026400"), (2.0, "02c800"), (5.95, "025302"), (12.0, "02b004")])
def test_set_speed(kmh, expected):
    # 02 53 02 (5.95) is exactly what the KS Fit slider sent
    assert p.cmd_set_speed(kmh) == H(expected)


def test_control_response():
    ok = p.parse_control_response(H("80020153 02".replace(" ", "")))
    assert ok.request == 0x02 and ok.ok and ok.params == H("5302")
    # STOP while the belt is running is refused
    bad = p.parse_control_response(H("80080401"))
    assert bad.request == 0x08 and bad.result == p.Result.OPERATION_FAILED and not bad.ok
    assert p.parse_control_response(H("0102")) is None


# --------------------------------------------------------------- Treadmill Data


def test_treadmill_data_idle():
    td = p.parse_treadmill_data(H("84240000000000000000000000000000 00".replace(" ", "")))
    assert td.flags == 0x2484 and td.speed_kmh == 0 and not td.moving


def test_treadmill_data_walking():
    # user walking at 3.5 km/h: 10 m, 1 kcal, 40 s, 7 steps
    td = p.parse_treadmill_data(H("84245e010a000001000000002800070000"))
    assert td.speed_kmh == 3.5
    assert td.distance_m == 10
    assert td.kcal == 1 and td.kcal_per_hour == 0 and td.kcal_per_min == 0
    assert td.elapsed_s == 40
    assert td.steps == 7
    assert td.moving


def test_treadmill_data_end_of_session():
    # final frame of a 49 s / 23 step session (matched the KS Fit app's summary)
    td = p.parse_treadmill_data(H("84240a000a000001000000003100170000"))
    assert (td.elapsed_s, td.steps, td.distance_m) == (49, 23, 10)


def test_treadmill_data_truncated():
    with pytest.raises(ValueError):
        p.parse_treadmill_data(H("8424640000"))


# ------------------------------------------------------------- status channels


def test_machine_status_remote_speed():
    ms = p.parse_machine_status(H("055802"))
    assert ms.event == p.MachineEvent.TARGET_SPEED_CHANGED and ms.target_speed_kmh == 6.0


def test_machine_status_pause_and_stop():
    assert p.parse_machine_status(H("0202")).stopped is False
    assert p.parse_machine_status(H("0201")).stopped is True
    assert p.parse_machine_status(H("04")).event == p.MachineEvent.STARTED_OR_RESUMED


def test_training_status_countdown():
    ts = p.parse_training_status(H("030e33000000"))
    assert ts.state == p.TrainingState.COUNTDOWN and ts.text == "3"
    assert p.parse_training_status(H("010d")).state == p.TrainingState.RUNNING
    assert p.parse_training_status(H("010f")).state == p.TrainingState.PAUSED
    assert p.parse_training_status(H("0101")).state == p.TrainingState.IDLE


# ------------------------------------------------------------------ Supplement


def test_handshake_frame_matches_app():
    assert p.cmd_handshake(H("87b4525133")) == H("71000587b452513387")
    assert p.cmd_handshake(H("fa27535133")) == H("710005fa275351336e")


def test_handshake_ack():
    f = p.parse_supp_frame(H("718000f1"))
    assert (f.cmd, f.flags, f.payload, f.checksum_ok) == (0x71, 0x80, b"", True)


def test_time_sync_frame_matches_app():
    when = datetime.fromtimestamp(0x6B007E0B, timezone.utc)
    assert p.cmd_time_sync(when) == H("7101080b7e006b010000006f")


def test_set_property_frame_matches_app():
    # KS Fit trying to raise the speed limit to 12.0 (refused: limit follows the handle)
    assert p.cmd_set_property(p.Prop.MAX_LIMIT_SPEED, 120) == H("720103077800f5")
    refused = p.parse_supp_frame(H("7281020707 03".replace(" ", "")))
    assert refused.checksum_ok and refused.payload == H("0707")


HANDLE_DOWN = H("728024010000000400730005000000020" "03c4008000f000a0000030600000009000500070" "03c008c")
HANDLE_UP = H("72802401000000040073000500000002003c4008000f000a0000030600000009000d0007007800d0")


def test_property_list_handle_down():
    f = p.parse_supp_frame(HANDLE_DOWN)
    assert f.checksum_ok
    props = p.Properties(p.parse_properties(f.payload))
    assert props.max_speed_kmh == 6.0
    assert props.handle_up is False
    assert props.miles is False and props.child_lock is False
    assert props.get(p.Prop.MOTOR_VERSION) == 115


def test_property_list_handle_up():
    props = p.Properties(p.parse_properties(p.parse_supp_frame(HANDLE_UP).payload))
    assert props.max_speed_kmh == 12.0
    assert props.handle_up is True
    assert props.named()["read_only"] == 13


def test_session_record():
    f = p.parse_supp_frame(H("73500d00117e006b01000000497e006b8a"))
    assert f.checksum_ok  # push frames checksum excludes byte 0
    rec = p.parse_session_record(f.payload)
    assert rec.start == datetime.fromtimestamp(0x6B007E11, timezone.utc)
    assert rec.end == datetime.fromtimestamp(0x6B007E49, timezone.utc)
    assert (rec.end - rec.start).total_seconds() == 56

    open_rec = p.parse_session_record(p.parse_supp_frame(H("73500d00117e006b0100000000000000 58".replace(" ", ""))).payload)
    assert open_rec.end is None


def test_bad_checksum_detected():
    assert not p.parse_supp_frame(H("718000f2")).checksum_ok
