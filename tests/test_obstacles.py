"""Obstacle log: the robot's reported clusters only, grouped by plan run."""

import json
from pathlib import Path

from yarbo_local.models import RobotState
from yarbo_local.obstacles import ObstacleTracker

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "protocol/fixtures/3.14.11/mower-pro-ultrasonic-avoidance.jsonl"
)


def test_close_ultrasonic_readings_do_not_become_obstacles() -> None:
    """The West Lawn window is full of close ultrasonic readings from uncut grass."""
    tracker = ObstacleTracker()
    state = RobotState()
    close_readings = 0
    for line in FIXTURE.read_text().splitlines():
        rec = json.loads(line)
        leaf = rec["topic"].rsplit("/", 1)[-1]
        if leaf == "plan_feedback":
            tracker.on_plan_feedback(rec["payload"], rec["t"])
        elif leaf == "cloud_points_feedback":
            tracker.on_barriers(rec["payload"], rec["t"])
        elif leaf == "DeviceMSG":
            state, _ = state.with_frame(rec["payload"], rec["t"])
            tracker.on_state(state, rec["t"])
            rf = state.get("ultrasonic_msg.rf_dis")
            close_readings += isinstance(rf, int) and 0 < rf < 1000
    assert close_readings > 10
    run = tracker.current
    assert run is not None
    assert run.plan_id == 2
    assert run.obstacle_count == 0


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
    assert tracker.on_state(idle, 103.0)
    assert tracker.current is None
    assert tracker.latest.ended == 103.0
    assert tracker.on_plan_feedback({"planId": 1, "startTime": 100}, 104.0)
    assert tracker.current is tracker.latest
    assert len(tracker.current.barriers) == 2
    assert tracker.on_plan_feedback({"planId": 2, "startTime": 200, "areaIds": [9]}, 105.0)
    assert tracker.runs[0].ended == 105.0
    assert tracker.current.obstacle_count == 0


def test_stored_ultrasonic_detections_are_dropped() -> None:
    stored = {
        "version": 1,
        "runs": [
            {
                "id": "2-1",
                "plan_id": 2,
                "area_ids": [9],
                "started": 1.0,
                "ended": None,
                "detections": [
                    {"t": 1, "source": "ultrasonic_right", "point": [1, 2], "robot": [0, 0, 0]}
                ],
                "barriers": [{"points": [[1, 2]], "first_seen": 1, "last_seen": 1}],
            }
        ],
    }
    assert ObstacleTracker.needs_migration(stored)
    tracker = ObstacleTracker.from_dict(stored)
    data = tracker.to_dict()
    assert not ObstacleTracker.needs_migration(data)
    assert data["runs"][0]["barriers"] == [
        {"points": [[1.0, 2.0]], "first_seen": 1.0, "last_seen": 1.0}
    ]
    assert ObstacleTracker.from_dict(json.loads(json.dumps(data))).to_dict() == data


def test_same_run_id_does_not_start_a_new_run() -> None:
    tracker = ObstacleTracker()
    assert tracker.on_plan_feedback({"planId": 2, "startTime": 1789495163}, 1.0)
    assert not tracker.on_plan_feedback({"planId": 2, "startTime": 1789495163.0}, 2.0)
    assert len(tracker.runs) == 1
