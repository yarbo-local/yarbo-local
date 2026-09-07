"""Topic helpers for the ``snowbot/{serial}/{side}/{leaf}`` namespace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PREFIX = "snowbot"
Side = Literal["app", "device"]

# Device-side leaves seen across the vendor SDK, python-yarbo and steves2j.
KNOWN_DEVICE_LEAVES: frozenset[str] = frozenset(
    {
        "DeviceMSG",
        "heart_beat",
        "data_feedback",
        "plan_feedback",
        "recharge_feedback",
        "cloud_points_feedback",
        "patrol_feedback",
        "ota_feedback",
        "deviceinfo_feedback",
        "log_feedback",
        "log",
        "begin_update",
        "shutdown_signal",
        "combined_odom_path",
    }
)


@dataclass(frozen=True, slots=True)
class Topic:
    """A parsed Yarbo topic."""

    serial: str
    side: Side
    leaf: str

    def __str__(self) -> str:
        return f"{PREFIX}/{self.serial}/{self.side}/{self.leaf}"


def parse(topic: str) -> Topic | None:
    """Parse ``snowbot/<serial>/<app|device>/<leaf>``; None for anything else."""
    parts = topic.split("/", 3)
    if len(parts) != 4 or parts[0] != PREFIX or parts[2] not in ("app", "device"):
        return None
    side: Side = "app" if parts[2] == "app" else "device"
    return Topic(serial=parts[1], side=side, leaf=parts[3])


def app(serial: str, command: str) -> str:
    """Command topic for a serial."""
    return f"{PREFIX}/{serial}/app/{command}"


def device(serial: str, leaf: str) -> str:
    """Telemetry topic for a serial."""
    return f"{PREFIX}/{serial}/device/{leaf}"


def all_for(serial: str | None = None) -> str:
    """Wildcard covering both sides for one serial, or every serial."""
    return f"{PREFIX}/{serial or '+'}/#"
