"""Obstacle tracking on a real run: the West Lawn recording with ultrasonic avoidance."""

import json
from pathlib import Path

from yarbo_local.models import RobotState
from yarbo_local.obstacles import ObstacleTracker, estimate_point

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "protocol/fixtures/3.14.11/mower-pro-ultrasonic-avoidance.jsonl"
)


def _replay(tracker: ObstacleTracker) -> list:
    state = RobotState()
    new = []
    for line in FIXTURE.read_text().splitlines():
        rec = json.loads(line)
        leaf = rec["topic"].rsplit("/", 1)[-1]
        if leaf == "plan_feedback":
            tracker.on_plan_feedback(rec["payload"], rec["t"])
        elif leaf == "cloud_points_feedback":
            tracker.on_barriers(rec["payload"], rec["t"])
        elif leaf == "DeviceMSG":
            state, _ = state.with_frame(rec["payload"], rec["t"])
            new += tracker.on_state(state, rec["t"])
    return new


def test_ultrasonic_avoidance_becomes_obstacles_in_the_run() -> None:
    tracker = ObstacleTracker()
    new = _replay(tracker)
    run = tracker.current
    assert run is not None
    assert run.plan_id == 2
    assert run.area_ids == [9]
    assert run.barriers == []  # the robot's own obstacle list was empty in this window
    assert len(new) >= 3
    assert len(run.detections) == len(new)
    for d in run.detections:
        assert d.source in {"ultrasonic_left", "ultrasonic_right", "ultrasonic_middle"}
        assert 0.05 <= d.distance_m <= 4.0
        # Estimated points sit within a couple of metres of where the robot was.
        assert ((d.point[0] - d.robot[0]) ** 2 + (d.point[1] - d.robot[1]) ** 2) ** 0.5 < 5
    # Distinct obstacles are at least the merge distance apart.
    pts = [d.point for d in run.detections]
    assert all(
        ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 >= 0.5
        for i, a in enumerate(pts)
        for b in pts[i + 1 :]
    )


def test_log_roundtrips_and_a_new_run_starts_fresh() -> None:
    tracker = ObstacleTracker()
    _replay(tracker)
    restored = ObstacleTracker.from_dict(json.loads(json.dumps(tracker.to_dict())))
    assert restored.to_dict() == tracker.to_dict()

    run_id = restored.current.id
    assert restored.on_plan_feedback({"planId": 1, "startTime": 5, "areaIds": [4]}, 10.0)
    assert restored.get(run_id).ended == 10.0
    assert restored.current.plan_id == 1
    assert restored.current.obstacle_count == 0


def test_barrier_clusters_merge_and_run_end_reopen() -> None:
    tracker = ObstacleTracker()
    tracker.on_plan_feedback({"planId": 1, "startTime": 100, "areaIds": [4]}, 100.0)
    cluster = [[{"x": 1.0, "y": 2.0}, {"x": 1.1, "y": 2.1}], [{"x": 5.0, "y": 5.0}]]
    assert len(tracker.on_barriers({"tmp_barrier_points": cluster}, 101.0)) == 2
    assert tracker.on_barriers({"tmp_barrier_points": cluster}, 102.0) == []
    assert tracker.current.barriers[0].last_seen == 102.0
    idle, _ = RobotState().with_frame(
        {"StateMSG": {"on_going_planning": 0, "on_going_recharging": 0}}, 103.0
    )
    tracker.on_state(idle, 103.0)
    assert tracker.current is None
    assert tracker.latest.ended == 103.0
    # The robot resumes the same run after a recharge: the same id reopens it.
    assert tracker.on_plan_feedback({"planId": 1, "startTime": 100}, 104.0)
    assert tracker.current is tracker.latest
    assert len(tracker.current.barriers) == 2


def test_estimate_point_is_ahead_of_the_robot() -> None:
    x, y = estimate_point("middle", 1.0, (0.0, 0.0, 0.0))
    assert round(x, 2) == 1.7
    assert y == 0.0
    _, ly = estimate_point("left", 1.0, (0.0, 0.0, 0.0))
    _, ry = estimate_point("right", 1.0, (0.0, 0.0, 0.0))
    assert ly > 0 > ry


def test_same_run_id_does_not_start_a_new_run() -> None:
    tracker = ObstacleTracker()
    assert tracker.on_plan_feedback({"planId": 2, "startTime": 1789495163}, 1.0)
    assert not tracker.on_plan_feedback({"planId": 2, "startTime": 1789495163.0}, 2.0)
    assert len(tracker.runs) == 1
