"""Plan lifecycle: when a run started, paused, resumed and ended, and why.

Automations need this more than anything else the robot reports. "Mow every
three days unless it rained" is only safe to write when "the last run finished"
means what it says.

The rules below are split by how we know them.

Seen on firmware 3.14.11 (fixtures ``mower-pro-run-faults-estop.jsonl`` and
``return-to-dock-from-fault.jsonl``):

- A running plan reports ``on_going_planning`` 1 (working) or 3 (heading to the area).
- A paused plan reports ``on_going_planning`` **0** with ``planning_paused`` set: 7 with
  a fault, 4 after an emergency stop. The vendor's rule, "running and paused", never
  matches, so a transition to 0 is not the end of a run.
- ``plan_feedback`` goes silent while paused and starts again on resume, with the same
  ``planId`` and ``startTime``. Its silence is not the end of a run either.
- Sending the robot home from a paused plan ends the run: ``on_going_recharging`` goes to
  1 while ``planning_paused`` still reads 7, and the pause code only clears about 15 s
  after docking.

- A start reports ``on_going_planning`` 2 for a second, then 3, then 1 on reaching the area
  (fixture ``plan-start-from-dock.jsonl``). A pause from the app sets ``planning_paused`` 1.
- A completed plan says so in its last ``plan_feedback``: ``state`` 5, every area in
  ``finishIds``, ``cleanAreaId`` -1, all of the area finished. The state frames never show 5:
  they go from 1 to 0 with ``on_going_recharging`` already set, because the robot heads home
  by itself (fixture ``plan-completes-and-docks.jsonl``). Between the edge pass and the fill
  the code goes 1, 3, 1 within the same run.

Borrowed, not yet seen here (vendor SDK and the jtubb fork):

- ``on_going_planning`` 5 in a state frame means completed.
- A robot that runs low mid-plan returns to charge and resumes by itself. That is told
  apart from an ended run by ``planning_paused`` 2, or by progress below 99 percent.
- A plan stopped from the app drops to planning 0 with no pause code.

Nothing here does I/O or reads a clock; times are passed in.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from .feedback import PlanFeedback
from .models import PLANNING_COMPLETED, PLANNING_RUNNING, RobotState

LOW_BATTERY_PAUSE = 2
NEARLY_DONE = 99.0
ANNOUNCE_AFTER = 5.0  # seconds to wait for plan_feedback before reporting a start without it
CONFIRM_WITHIN = 3.0  # seconds a run heard of through feedback may wait for a state frame


class Phase(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    RECHARGING = "recharging"  # mid-plan, will resume by itself


class EventKind(StrEnum):
    STARTED = "plan_started"
    PAUSED = "plan_paused"
    RESUMED = "plan_resumed"
    FINISHED = "plan_finished"


class FinishReason(StrEnum):
    COMPLETED = "completed"
    RETURNED_TO_DOCK = "returned_to_dock"  # sent home, or gave up, before finishing
    STOPPED = "stopped"
    SUPERSEDED = "superseded"  # another run began before this one ended


@dataclass(frozen=True, slots=True)
class Run:
    """One run of one plan, from start to finish, however often it paused."""

    run_id: str | None  # planId-startTime once plan_feedback has been heard
    plan_id: int | None
    phase: Phase
    since: float
    first_seen: float
    adopted: bool  # already under way when we started watching, so no start was reported
    progress: float | None = None
    duration_s: int | None = None
    pause_reason: str | None = None
    fault_code: int | None = None
    pauses: int = 0
    announced: bool = True  # False while a start waits for plan_feedback to name the plan
    confirmed: bool = True  # False while a run known only from feedback awaits a state frame


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    kind: EventKind
    at: float
    run_id: str | None
    plan_id: int | None
    reason: str | None = None
    progress: float | None = None
    duration_s: int | None = None
    fault_code: int | None = None
    fault: str | None = None

    def attributes(self) -> dict[str, Any]:
        """For an event entity: everything but the kind, without the empty fields."""
        return {k: v for k, v in asdict(self).items() if k != "kind" and v is not None}


@dataclass(frozen=True, slots=True)
class _Codes:
    planning: int
    paused: int
    recharging: int
    charging: bool

    @property
    def running(self) -> bool:
        return self.planning in PLANNING_RUNNING

    @property
    def completed(self) -> bool:
        return self.planning in PLANNING_COMPLETED

    @property
    def held(self) -> bool:
        """Paused in place: a pause code, and not on the way home or on the dock."""
        return self.paused > 0 and self.recharging == 0 and not self.charging


def _codes(state: RobotState) -> _Codes:
    return _Codes(state.planning_code, state.paused_code, state.recharging_code, state.charging)


class PlanTracker:
    """Follows one robot's runs. Feed it every state and every ``plan_feedback``."""

    def __init__(self) -> None:
        self.current: Run | None = None
        self.last: Run | None = None
        self.last_finish_reason: FinishReason | None = None
        self.last_completed: dict[int, float] = {}  # plan id -> when it last completed
        self._seen_state = False  # has anything at all been heard yet

    # -- persistence: what must survive a restart for "every N days" to work

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "last_completed": {str(k): v for k, v in self.last_completed.items()}}

    @classmethod
    def from_dict(cls, data: Any) -> PlanTracker:
        tracker = cls()
        if isinstance(data, dict) and isinstance(data.get("last_completed"), dict):
            for key, value in data["last_completed"].items():
                if str(key).lstrip("-").isdigit() and isinstance(value, int | float):
                    tracker.last_completed[int(key)] = float(value)
        return tracker

    # -- inputs

    def on_plan_feedback(self, feedback: PlanFeedback, at: float) -> list[LifecycleEvent]:
        events: list[LifecycleEvent] = []
        run = self.current
        first, self._seen_state = not self._seen_state, True
        if run is None and self.last is not None and feedback.run_id == self.last.run_id:
            return events  # a straggler from the run that just ended, not a new one
        if run is not None and run.run_id and feedback.run_id and run.run_id != feedback.run_id:
            events.append(self._finish(run, FinishReason.SUPERSEDED, at))
            run = None
        if run is None:
            # Feedback only flows while a plan moves, so a run has begun. Only the very
            # first thing we ever hear can be a run that was already under way.
            adopted = first and not events
            # Feedback comes at 2 Hz and state at 1 Hz, so the state may still say idle for a
            # moment. Until a frame agrees, that is not evidence the run has stopped.
            run = Run(
                None,
                None,
                Phase.RUNNING,
                at,
                at,
                adopted=adopted,
                announced=adopted,
                confirmed=False,
            )
        if not run.announced:
            run = replace(run, announced=True)
            events.append(LifecycleEvent(EventKind.STARTED, at, feedback.run_id, feedback.plan_id))
        self.current = replace(
            run,
            run_id=feedback.run_id or run.run_id,
            plan_id=feedback.plan_id if feedback.plan_id is not None else run.plan_id,
            progress=feedback.progress if feedback.progress is not None else run.progress,
            duration_s=feedback.duration_s if feedback.duration_s is not None else run.duration_s,
        )
        if feedback.state in PLANNING_COMPLETED:
            # Seen on 3.14.11: completion is said here and only here. The 1 Hz state never shows
            # 5; it goes from working straight to 0 with the robot already on its way home.
            events.append(self._finish(self.current, FinishReason.COMPLETED, at))
        return events

    def on_state(self, state: RobotState, at: float) -> list[LifecycleEvent]:
        codes = _codes(state)
        first = not self._seen_state
        self._seen_state = True
        run = self.current
        if run is None:
            return self._maybe_begin(codes, state, at, adopted=first)
        if run.phase is Phase.RUNNING:
            if codes.running and not run.confirmed:
                run = self.current = replace(run, confirmed=True)
            elif not run.confirmed and at - run.first_seen < CONFIRM_WITHIN:
                return []
            events = self._from_running(run, codes, state, at)
            if not events and not run.announced and at - run.first_seen > ANNOUNCE_AFTER:
                self.current = replace(run, announced=True)
                events = [LifecycleEvent(EventKind.STARTED, at, run.run_id, run.plan_id)]
            return events
        return self._from_waiting(run, codes, state, at)

    # -- transitions

    def _maybe_begin(
        self, codes: _Codes, state: RobotState, at: float, *, adopted: bool
    ) -> list[LifecycleEvent]:
        if codes.running:
            # A real start is reported once plan_feedback has named the plan.
            self.current = Run(
                None, None, Phase.RUNNING, at, at, adopted=adopted, announced=adopted
            )
            return []
        if adopted and codes.held:
            # Home Assistant restarted while a plan sat paused. Pick it up quietly.
            self.current = self._paused(
                Run(None, None, Phase.PAUSED, at, at, adopted=True), state, at
            )
        return []

    def _from_running(
        self, run: Run, codes: _Codes, state: RobotState, at: float
    ) -> list[LifecycleEvent]:
        if codes.running:
            return []
        if codes.completed:
            return [self._finish(run, FinishReason.COMPLETED, at)]
        if codes.recharging > 0:
            if codes.paused == LOW_BATTERY_PAUSE or (run.progress or 0.0) < NEARLY_DONE:
                return [self._pause(run, state, at, Phase.RECHARGING, "low_battery_recharging")]
            return [self._finish(run, FinishReason.COMPLETED, at)]
        if codes.paused > 0:
            return [self._pause(run, state, at, Phase.PAUSED, state.pause_reason or "unknown")]
        return [self._finish(run, FinishReason.STOPPED, at)]

    def _from_waiting(  # noqa: PLR0911 - one return per way out of a pause, by design
        self, run: Run, codes: _Codes, state: RobotState, at: float
    ) -> list[LifecycleEvent]:
        if codes.running:
            self.current = replace(
                run, phase=Phase.RUNNING, since=at, pause_reason=None, fault_code=None
            )
            return [self._event(EventKind.RESUMED, self.current, at)]
        if codes.completed:
            return [self._finish(run, FinishReason.COMPLETED, at)]
        if run.phase is Phase.PAUSED and (codes.recharging > 0 or codes.charging):
            if codes.paused == LOW_BATTERY_PAUSE:
                self.current = replace(run, phase=Phase.RECHARGING, since=at)
                return []
            return [self._finish(run, FinishReason.RETURNED_TO_DOCK, at)]
        if run.phase is Phase.PAUSED and codes.paused == 0:
            return [self._finish(run, FinishReason.STOPPED, at)]
        if run.phase is Phase.RECHARGING and not (
            codes.recharging or codes.charging or codes.paused
        ):
            return [self._finish(run, FinishReason.STOPPED, at)]
        if run.phase is Phase.PAUSED and state.pause_reason != run.pause_reason:
            self.current = self._paused(run, state, run.since)  # the reason changed in place
        return []

    # -- helpers

    def _paused(self, run: Run, state: RobotState, at: float) -> Run:
        return replace(
            run,
            phase=Phase.PAUSED,
            since=at,
            pause_reason=state.pause_reason or "unknown",
            fault_code=state.error_code or None,
        )

    def _pause(
        self, run: Run, state: RobotState, at: float, phase: Phase, reason: str
    ) -> LifecycleEvent:
        self.current = replace(
            run,
            phase=phase,
            since=at,
            pause_reason=reason,
            fault_code=state.error_code or None,
            pauses=run.pauses + 1,
        )
        fault = state.fault
        return replace(
            self._event(EventKind.PAUSED, self.current, at, reason),
            fault_code=fault.code if fault else None,
            fault=fault.description if fault else None,
        )

    def _finish(self, run: Run, reason: FinishReason, at: float) -> LifecycleEvent:
        self.last, self.last_finish_reason, self.current = run, reason, None
        if reason is FinishReason.COMPLETED and run.plan_id is not None:
            self.last_completed[run.plan_id] = at
        return self._event(EventKind.FINISHED, run, at, reason.value)

    @staticmethod
    def _event(kind: EventKind, run: Run, at: float, reason: str | None = None) -> LifecycleEvent:
        return LifecycleEvent(
            kind=kind,
            at=at,
            run_id=run.run_id,
            plan_id=run.plan_id,
            reason=reason,
            progress=run.progress,
            duration_s=run.duration_s,
        )
