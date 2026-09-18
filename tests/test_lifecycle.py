"""Plan lifecycle, replayed from a real afternoon and from the rules we only borrowed."""

from __future__ import annotations

import json
from pathlib import Path

from yarbo_local.feedback import BarrierPoints, PlanFeedback, RechargeFeedback
from yarbo_local.lifecycle import EventKind, FinishReason, LifecycleEvent, Phase, PlanTracker
from yarbo_local.models import Activity, RobotState

FIXTURES = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "3.14.11"
RUN = FIXTURES / "mower-pro-run-faults-estop.jsonl"
HOME = FIXTURES / "return-to-dock-from-fault.jsonl"


def replay(tracker: PlanTracker, *files: Path) -> tuple[list[LifecycleEvent], list[Activity]]:
    events: list[LifecycleEvent] = []
    activities: list[Activity] = []
    state = RobotState()
    for file in files:
        for line in file.read_text().splitlines():
            rec = json.loads(line)
            leaf = rec["topic"].rsplit("/", 1)[-1]
            if "/device/" not in rec["topic"]:
                continue
            if leaf == "DeviceMSG":
                state, _ = state.with_frame(rec["payload"], rec["t"])
                events += tracker.on_state(state, rec["t"])
                if not activities or activities[-1] is not state.activity:
                    activities.append(state.activity)
            elif leaf == "plan_feedback":
                feedback = PlanFeedback.from_wire(rec["payload"])
                assert feedback is not None
                events += tracker.on_plan_feedback(feedback, rec["t"])
    return events, activities


def state_of(**codes: int) -> RobotState:
    frame = {"StateMSG": codes, "BatteryMSG": {"status": codes.pop("battery_status", 1)}}
    return RobotState().with_frame(frame, 0.0)[0]


# -- the real afternoon: West Lawn, eight tilt faults, one emergency stop, then sent home


def test_the_west_lawn_afternoon() -> None:
    tracker = PlanTracker()
    events, _ = replay(tracker, RUN)

    # We started watching mid-run, so there is no start to report and nothing is invented.
    assert [e.kind for e in events].count(EventKind.STARTED) == 0
    paused = [e for e in events if e.kind is EventKind.PAUSED]
    resumed = [e for e in events if e.kind is EventKind.RESUMED]
    assert [e.reason for e in paused] == ["fault"] * 7 + ["emergency_stop"] + ["fault"]
    assert len(resumed) == 8
    assert all(
        e.fault == "Tilted or flipped over" and e.fault_code == 902
        for e in paused
        if e.reason == "fault"
    )
    assert paused[7].fault is None, "an emergency stop is not a fault"

    # One run throughout: every event carries the same identity, from plan_feedback.
    assert len({e.run_id for e in events}) == 1
    assert {e.plan_id for e in events} == {2}
    assert not [e for e in events if e.kind is EventKind.FINISHED], "a pause is not an end"

    run = tracker.current
    assert run is not None
    assert run.phase is Phase.PAUSED
    assert run.pause_reason == "fault"
    assert run.pauses == 9
    assert run.progress == 89.3


def test_sending_a_paused_robot_home_ends_the_run() -> None:
    tracker = PlanTracker()
    events, _ = replay(tracker, RUN, HOME)
    finished = [e for e in events if e.kind is EventKind.FINISHED]
    assert len(finished) == 1
    end = finished[0]
    assert end.reason == FinishReason.RETURNED_TO_DOCK
    assert end.progress == 89.3
    assert end.plan_id == 2
    assert tracker.current is None
    assert tracker.last_completed == {}, "89 percent and a trip home is not a completed run"


def test_activity_through_the_same_afternoon() -> None:
    _, activities = replay(PlanTracker(), RUN, HOME)
    assert Activity.IDLE not in activities, "a paused or faulted plan must never read as idle"
    assert Activity.PAUSED in activities, "the emergency stop is a pause with planning at 0"
    assert activities[0] is Activity.WORKING
    assert Activity.RETURNING in activities
    # Known transient, from the real capture: for about 15 s after docking, before charging
    # starts, the pause code still reads 7 with nothing else set, so one frame reads
    # "paused". The tracker is not fooled; the run ended when the robot set off home.
    assert activities[-2:] == [Activity.RETURNING, Activity.PAUSED]


def test_restarting_while_paused_picks_the_run_up_quietly() -> None:
    tracker = PlanTracker()
    assert (
        tracker.on_state(state_of(on_going_planning=0, planning_paused=7, error_code=902), 1.0)
        == []
    )
    assert tracker.current is not None
    assert tracker.current.phase is Phase.PAUSED
    assert tracker.current.adopted
    events = tracker.on_state(state_of(on_going_planning=3), 2.0)
    assert [e.kind for e in events] == [EventKind.RESUMED]


def test_a_stale_pause_code_on_the_dock_is_not_a_paused_plan() -> None:
    tracker = PlanTracker()
    docked = state_of(on_going_planning=0, planning_paused=7, battery_status=3)
    assert tracker.on_state(docked, 1.0) == []
    assert tracker.current is None
    assert docked.activity is Activity.CHARGING


# -- a start is reported once, with the plan's name on it


def test_a_start_waits_for_plan_feedback_to_name_the_plan() -> None:
    tracker = PlanTracker()
    tracker.on_state(state_of(on_going_planning=0), 1.0)
    assert tracker.on_state(state_of(on_going_planning=2), 2.0) == []  # calculating route
    feedback = PlanFeedback.from_wire({"planId": 7, "startTime": 1000, "totalCleanArea": 10.0})
    assert feedback is not None
    events = tracker.on_plan_feedback(feedback, 2.4)
    assert [(e.kind, e.plan_id, e.run_id) for e in events] == [(EventKind.STARTED, 7, "7-1000")]
    assert tracker.on_plan_feedback(feedback, 2.9) == []
    assert tracker.on_state(state_of(on_going_planning=1), 3.0) == []


def test_feedback_first_still_reports_one_start() -> None:
    tracker = PlanTracker()
    tracker.on_state(state_of(on_going_planning=0), 1.0)
    feedback = PlanFeedback.from_wire({"planId": 7, "startTime": 1000})
    assert feedback is not None
    assert [e.kind for e in tracker.on_plan_feedback(feedback, 1.5)] == [EventKind.STARTED]
    assert tracker.on_state(state_of(on_going_planning=1), 2.0) == []


def test_feedback_ahead_of_a_stale_idle_frame_is_not_a_stop() -> None:
    """Feedback runs at 2 Hz and state at 1 Hz: at a start the state can lag by a frame."""
    tracker = PlanTracker()
    tracker.on_state(state_of(on_going_planning=0), 1.0)
    feedback = PlanFeedback.from_wire({"planId": 7, "startTime": 1000})
    assert feedback is not None
    assert [e.kind for e in tracker.on_plan_feedback(feedback, 1.4)] == [EventKind.STARTED]
    assert tracker.on_state(state_of(on_going_planning=0), 2.0) == [], "one stale frame"
    assert tracker.on_state(state_of(on_going_planning=1), 3.0) == []
    assert tracker.current is not None
    # But feedback that no state frame ever backs up does not hold a run open for ever.
    lone = PlanTracker()
    lone.on_state(state_of(on_going_planning=0), 1.0)
    lone.on_plan_feedback(feedback, 1.4)
    events = lone.on_state(state_of(on_going_planning=0), 9.0)
    assert [(e.kind, e.reason) for e in events] == [(EventKind.FINISHED, "stopped")]


def test_a_start_without_feedback_is_still_reported() -> None:
    tracker = PlanTracker()
    tracker.on_state(state_of(on_going_planning=0), 1.0)
    tracker.on_state(state_of(on_going_planning=1), 2.0)
    assert tracker.on_state(state_of(on_going_planning=1), 4.0) == []
    events = tracker.on_state(state_of(on_going_planning=1), 8.0)
    assert [(e.kind, e.plan_id) for e in events] == [(EventKind.STARTED, None)]


# -- borrowed rules, not yet seen on our robot: completion, low battery, stop


def _running(plan_id: int = 3, progress_area: float = 50.0) -> PlanTracker:
    tracker = PlanTracker()
    tracker.on_state(state_of(on_going_planning=0), 1.0)
    tracker.on_state(state_of(on_going_planning=1), 2.0)
    feedback = PlanFeedback.from_wire(
        {
            "planId": plan_id,
            "startTime": 99,
            "totalCleanArea": 100.0,
            "finishCleanArea": progress_area,
        }
    )
    assert feedback is not None
    tracker.on_plan_feedback(feedback, 2.5)
    return tracker


def test_completion_is_remembered_per_plan_and_survives_a_restart() -> None:
    tracker = _running(plan_id=3, progress_area=100.0)
    events = tracker.on_state(state_of(on_going_planning=5), 60.0)
    assert [(e.kind, e.reason) for e in events] == [(EventKind.FINISHED, "completed")]
    assert tracker.last_completed == {3: 60.0}
    restored = PlanTracker.from_dict(json.loads(json.dumps(tracker.to_dict())))
    assert restored.last_completed == {3: 60.0}
    assert PlanTracker.from_dict({"last_completed": {"x": "y"}}).last_completed == {}


def test_a_low_battery_trip_home_is_not_the_end() -> None:
    tracker = _running(progress_area=40.0)
    events = tracker.on_state(
        state_of(on_going_planning=0, planning_paused=2, on_going_recharging=1), 10.0
    )
    assert [(e.kind, e.reason) for e in events] == [(EventKind.PAUSED, "low_battery_recharging")]
    assert tracker.current is not None
    assert tracker.current.phase is Phase.RECHARGING
    charging = state_of(on_going_planning=0, planning_paused=2, battery_status=3)
    assert tracker.on_state(charging, 600.0) == []
    events = tracker.on_state(state_of(on_going_planning=3), 4000.0)
    assert [e.kind for e in events] == [EventKind.RESUMED]


def test_returning_at_full_progress_counts_as_completed() -> None:
    tracker = _running(progress_area=99.5)
    events = tracker.on_state(state_of(on_going_planning=0, on_going_recharging=1), 10.0)
    assert [(e.kind, e.reason) for e in events] == [(EventKind.FINISHED, "completed")]


def test_a_stop_ends_the_run_and_stragglers_do_not_restart_it() -> None:
    tracker = _running()
    events = tracker.on_state(state_of(on_going_planning=0), 10.0)
    assert [(e.kind, e.reason) for e in events] == [(EventKind.FINISHED, "stopped")]
    late = PlanFeedback.from_wire({"planId": 3, "startTime": 99})
    assert late is not None
    assert tracker.on_plan_feedback(late, 10.3) == []
    assert tracker.current is None


def test_a_new_run_supersedes_one_we_never_saw_end() -> None:
    tracker = _running(plan_id=3)
    other = PlanFeedback.from_wire({"planId": 4, "startTime": 500})
    assert other is not None
    events = tracker.on_plan_feedback(other, 20.0)
    assert [(e.kind, e.plan_id, e.reason) for e in events] == [
        (EventKind.FINISHED, 3, "superseded"),
        (EventKind.STARTED, 4, None),
    ]


# -- the typed feedback models against real messages


def test_plan_feedback_from_the_wire() -> None:
    first = next(
        json.loads(line)["payload"]
        for line in RUN.read_text().splitlines()
        if "/device/plan_feedback" in line
    )
    feedback = PlanFeedback.from_wire(first)
    assert feedback is not None
    assert feedback.plan_id == 2
    assert feedback.area_ids == (9,)
    assert feedback.run_id == f"2-{first['startTime']}"
    assert feedback.progress == 49.9
    assert feedback.duration_s == 4386
    assert feedback.areas[0].area_id == 9
    assert feedback.areas[0].clean_index == first["cleanPathProgress"][0]["clean_index"]
    assert PlanFeedback.from_wire({"no": "plan"}) is None
    assert PlanFeedback.from_wire("nonsense") is None


def test_progress_is_bounded_and_absent_without_a_total() -> None:
    over = PlanFeedback.from_wire({"planId": 1, "totalCleanArea": 10.0, "finishCleanArea": 12.0})
    assert over is not None
    assert over.progress == 100.0
    empty = PlanFeedback.from_wire({"planId": 1, "totalCleanArea": 0})
    assert empty is not None
    assert empty.progress is None


def test_recharge_and_barrier_feedback_from_the_wire() -> None:
    route = next(
        json.loads(line)["payload"]
        for line in HOME.read_text().splitlines()
        if "/device/recharge_feedback" in line
    )
    recharge = RechargeFeedback.from_wire(route)
    assert recharge is not None
    assert len(recharge.path) == len(route["path"])
    assert recharge.state == 1
    assert RechargeFeedback.from_wire({}) is None

    barriers = BarrierPoints.from_wire(
        {
            "rotate_rad": 0.5,
            "tmp_barrier_points": [[{"x": 1, "y": 2}, {"x": 1.1, "y": 2}], [], "junk"],
        }
    )
    assert barriers is not None
    assert barriers.clusters == (((1.0, 2.0), (1.1, 2.0)),)
    assert BarrierPoints.from_wire({"tmp_barrier_points": []}) == BarrierPoints(None, ())


# -- 2026-09-18: the first start, pause and completion seen on the wire

START = FIXTURES / "plan-start-from-dock.jsonl"
FAILED_START = FIXTURES / "plan-start-fails-route-wp005.jsonl"
PAUSE = FIXTURES / "plan-pause-resume-app.jsonl"
COMPLETES = FIXTURES / "plan-completes-and-docks.jsonl"


def test_a_real_start_is_one_started_event_naming_the_plan() -> None:
    events, activities = replay(PlanTracker(), START)
    assert [e.kind for e in events] == [EventKind.STARTED]
    assert events[0].plan_id == 1
    assert Activity.CALCULATING_ROUTE in activities
    assert activities[-1] is Activity.WORKING, "2, then 3, then 1 on reaching the area"


def test_a_start_the_robot_cannot_route_is_no_run_at_all() -> None:
    tracker = PlanTracker()
    events, _ = replay(tracker, FAILED_START)
    assert events == []
    assert tracker.current is None


def test_a_pause_from_the_app_is_a_manual_pause_and_a_resume() -> None:
    tracker = PlanTracker()
    events, _ = replay(tracker, PAUSE)
    assert [e.kind for e in events] == [EventKind.PAUSED, EventKind.RESUMED]
    assert events[0].reason == "manual"
    assert tracker.current is not None, "the run goes on"


def test_the_real_completion_is_completed_once_and_the_trip_home_adds_nothing() -> None:
    tracker = PlanTracker()
    events, activities = replay(tracker, COMPLETES)
    finished = [e for e in events if e.kind is EventKind.FINISHED]
    assert len(finished) == 1
    assert finished[0].reason == FinishReason.COMPLETED.value
    assert finished[0].progress == 100.0
    assert finished[0].plan_id == 1
    assert [e.kind for e in events] == [EventKind.FINISHED], (
        "the 1, 3, 1 between the edge pass and the fill is one run, not a pause or a new start"
    )
    assert tracker.current is None
    assert 1 in tracker.last_completed
    assert activities[-1] is Activity.CHARGING
    assert Activity.RETURNING in activities


def test_completion_is_taken_from_feedback_even_when_progress_falls_short() -> None:
    tracker = _running()
    feedback = PlanFeedback.from_wire(
        {"planId": 3, "startTime": 99, "state": 5, "totalCleanArea": 100.0, "finishCleanArea": 96.0}
    )
    assert feedback is not None
    events = tracker.on_plan_feedback(feedback, 500.0)
    assert [e.reason for e in events] == ["completed"]
    assert tracker.on_state(state_of(on_going_recharging=2), 501.0) == []


def test_a_stop_from_the_app_reads_as_a_pause_then_a_stop_four_seconds_later() -> None:
    """Effects only: the app's command went through the vendor's cloud and was not seen."""
    tracker = PlanTracker()
    events, activities = replay(tracker, FIXTURES / "plan-stopped-from-app-effects-only.jsonl")
    assert [(e.kind, e.reason) for e in events] == [
        (EventKind.PAUSED, "manual"),
        (EventKind.FINISHED, "stopped"),
    ]
    assert events[-1].progress is not None
    assert 45.0 < events[-1].progress < 55.0
    assert tracker.current is None
    assert tracker.last_completed == {}, "a stopped run is not a completed one"
    assert activities[-1] is Activity.IDLE, "it stays where it is; it does not go home"


def test_our_own_start_then_stop_is_a_run_that_ends_stopped() -> None:
    tracker = PlanTracker()
    events, activities = replay(tracker, FIXTURES / "plan-start-then-stop.jsonl")
    assert [(e.kind, e.reason) for e in events] == [
        (EventKind.STARTED, None),
        (EventKind.FINISHED, "stopped"),
    ]
    assert events[0].plan_id == 2
    assert activities[-1] is Activity.IDLE


def test_our_stop_while_mowing_ends_the_run_with_no_pause_in_between() -> None:
    tracker = PlanTracker()
    events, activities = replay(tracker, FIXTURES / "plan-stop-while-mowing.jsonl")
    assert [(e.kind, e.reason) for e in events if e.kind is not EventKind.STARTED] == [
        (EventKind.FINISHED, "stopped")
    ]
    assert Activity.WORKING in activities
    assert Activity.PAUSED not in activities
    assert activities[-1] is Activity.IDLE
