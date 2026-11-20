"""Control and read a KingSmith R3 walking pad over Bluetooth LE."""

from .client import ControlError, SpeedRange, WalkingPad, WalkingPadError, discover
from .protocol import MachineStatus, Properties, SessionRecord, TrainingStatus, TreadmillData

__all__ = [
    "ControlError",
    "MachineStatus",
    "Properties",
    "SessionRecord",
    "SpeedRange",
    "TrainingStatus",
    "TreadmillData",
    "WalkingPad",
    "WalkingPadError",
    "discover",
]
__version__ = "0.1.0"
