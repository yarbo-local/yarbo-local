"""Local-first library and tooling for Yarbo robots over their LAN MQTT broker.

The library talks to the anonymous broker on the robot and to nothing else.
``YarboRobot`` is the entry point; ``Session`` is the protocol layer under it;
``Simulator`` stands in for a robot when there is none on the bench.
"""

from __future__ import annotations

from .client import YarboRobot
from .exceptions import (
    CommandError,
    CommandRefusedError,
    ConnectionLostError,
    ControllerError,
    ReplyTimeoutError,
    RobotNotFoundError,
    YarboError,
)
from .models import (
    Activity,
    ChargingPoint,
    Feedback,
    GgaFix,
    GpsReference,
    PlanSummary,
    RobotState,
    SiteMap,
    Zone,
)
from .registry import Command, Registry
from .resolve import resolve
from .session import Session
from .simulator import Simulator
from .transport import FakeTransport, MqttTransport

__version__ = "0.1.0.dev0"

__all__ = [
    "Activity",
    "ChargingPoint",
    "Command",
    "CommandError",
    "CommandRefusedError",
    "ConnectionLostError",
    "ControllerError",
    "FakeTransport",
    "Feedback",
    "GgaFix",
    "GpsReference",
    "MqttTransport",
    "PlanSummary",
    "Registry",
    "ReplyTimeoutError",
    "RobotNotFoundError",
    "RobotState",
    "Session",
    "Simulator",
    "SiteMap",
    "YarboError",
    "YarboRobot",
    "Zone",
    "resolve",
]
