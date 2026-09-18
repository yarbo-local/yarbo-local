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
    PreflightError,
    ReplyTimeoutError,
    RobotNotFoundError,
    YarboError,
)
from .feedback import AreaProgress, BarrierPoints, PlanFeedback, RechargeFeedback
from .lifecycle import EventKind, FinishReason, LifecycleEvent, Phase, PlanTracker
from .models import (
    Activity,
    ChargingPoint,
    Fault,
    Feedback,
    GgaFix,
    GpsReference,
    PlanSummary,
    RobotState,
    SiteMap,
    Zone,
)
from .obstacles import Barrier, ObstacleTracker, Run
from .preflight import Action, Refusal
from .registry import Command, Registry
from .resolve import resolve
from .session import Session
from .simulator import Simulator
from .transport import FakeBroker, FakeTransport, MqttTransport

__version__ = "0.1.0.dev0"

__all__ = [
    "Action",
    "Activity",
    "AreaProgress",
    "Barrier",
    "BarrierPoints",
    "ChargingPoint",
    "Command",
    "CommandError",
    "CommandRefusedError",
    "ConnectionLostError",
    "ControllerError",
    "EventKind",
    "FakeBroker",
    "FakeTransport",
    "Fault",
    "Feedback",
    "FinishReason",
    "GgaFix",
    "GpsReference",
    "LifecycleEvent",
    "MqttTransport",
    "ObstacleTracker",
    "Phase",
    "PlanFeedback",
    "PlanSummary",
    "PlanTracker",
    "PreflightError",
    "RechargeFeedback",
    "Refusal",
    "Registry",
    "ReplyTimeoutError",
    "RobotNotFoundError",
    "RobotState",
    "Run",
    "Session",
    "Simulator",
    "SiteMap",
    "YarboError",
    "YarboRobot",
    "Zone",
    "resolve",
]
