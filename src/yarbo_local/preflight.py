"""Checks before a command that moves the robot: refuse what cannot work, and say why.

Pure functions over the state. Each refusal has a key a user interface can translate
and a plain sentence for logs and the command line. The vendor integration also refuses
to start while ``BodyMsg.recharge_state`` reads "wired charging"; that mapping is wrong
on 3.14.11 (see the findings), so it is deliberately not repeated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import RobotState


class Action(StrEnum):
    DOCK = "dock"
    RESUME = "resume"
    PAUSE = "pause"
    STOP = "stop"
    START = "start"


# The registry command behind each action. A control exists only while this is sendable.
COMMANDS: dict[Action, str] = {
    Action.DOCK: "cmd_recharge",
    Action.RESUME: "resume",
    Action.PAUSE: "pause",
    Action.STOP: "stop",
    Action.START: "start_plan",
}


@dataclass(frozen=True, slots=True)
class Refusal:
    key: str
    message: str


def _paused_in_place(state: RobotState) -> bool:
    return state.paused_code > 0 and not state.returning and not state.charging


def _dock(state: RobotState) -> list[Refusal]:
    if state.returning:
        return [Refusal("already_returning", "The robot is already on its way home.")]
    if state.charging:
        return [Refusal("already_docked", "The robot is on the dock, charging.")]
    return []  # a fault does not block the trip home: going home is what clears a latched one


def _resume(state: RobotState) -> list[Refusal]:
    if state.plan_running:
        return [Refusal("plan_running", "The plan is already running.")]
    if not _paused_in_place(state):
        return [Refusal("nothing_to_resume", "No plan is paused.")]
    return []


def _pause(state: RobotState) -> list[Refusal]:
    return [] if state.plan_running else [Refusal("no_plan_running", "No plan is running.")]


def _stop(state: RobotState) -> list[Refusal]:
    if state.plan_running or _paused_in_place(state):
        return []
    return [Refusal("no_plan_running", "No plan is running.")]


def _start(state: RobotState) -> list[Refusal]:
    """Every reason at once, so the user fixes them in one go."""
    refusals: list[Refusal] = []
    if state.plan_running or _paused_in_place(state):
        refusals.append(Refusal("plan_running", "A plan is already under way."))
    if state.returning:
        refusals.append(Refusal("returning", "The robot is on its way home."))
    if state.fault is not None:
        refusals.append(Refusal("fault_active", f"The robot reports: {state.fault.description}."))
    if not state.rtk_usable:
        refusals.append(Refusal("rtk_not_ready", "The RTK position fix is not good enough."))
    if state.head_type in (None, 0):
        refusals.append(Refusal("no_head", "No head is attached."))
    return refusals  # charging on the dock is fine: the robot undocks itself


_CHECKS = {
    Action.DOCK: _dock,
    Action.RESUME: _resume,
    Action.PAUSE: _pause,
    Action.STOP: _stop,
    Action.START: _start,
}


def check(action: Action, state: RobotState, *, connected: bool = True) -> list[Refusal]:
    """Every reason ``action`` would not work right now. Empty means go."""
    if not connected:
        return [Refusal("robot_offline", "The robot is not connected.")]
    return _CHECKS[action](state)
