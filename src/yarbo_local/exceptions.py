"""Exception hierarchy. Every error the library raises derives from YarboError."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class YarboError(Exception):
    """Base class for all library errors."""


class ConnectionLostError(YarboError):
    """The broker connection dropped. The session reconnects; consumers back off."""


class RobotNotFoundError(YarboError):
    """Address resolution found no broker carrying the expected serial."""


class CommandRefusedError(YarboError):
    """The registry refused to send: unknown name, unverified command, or unconfirmed risk."""


class CommandError(YarboError):
    """The robot answered with a non-zero state."""

    def __init__(self, command: str, state: int, msg: str) -> None:
        super().__init__(f"{command}: state={state} msg={msg!r}")
        self.command = command
        self.state = state
        self.msg = msg


class ReplyTimeoutError(YarboError, TimeoutError):
    """No correlated reply arrived in time."""


class ControllerError(YarboError):
    """The robot did not grant the controller role."""


class PreflightError(YarboError):
    """A command that moves the robot was refused before sending, with the reasons.

    ``refusals`` holds :class:`yarbo_local.preflight.Refusal` items, each with a ``key``
    a user interface can translate.
    """

    def __init__(self, action: str, refusals: Sequence[Any]) -> None:
        self.action = str(action)
        self.refusals = tuple(refusals)
        reasons = "; ".join(str(getattr(r, "message", r)) for r in self.refusals)
        super().__init__(f"{self.action} refused: {reasons}")
