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


def _clusters(*groups: list[tuple[float, float]]) -> dict:  # type: ignore[type-arg]
    return {"tmp_barrier_points": [[{"x": x, "y": y} for x, y in g] for g in groups]}


def test_overlapping_clusters_of_one_object_count_once() -> None:
    """2026-10-24: one tree barrier under a metre across arrived as 7 shifting clusters."""
    tracker = ObstacleTracker()
    tracker.on_plan_feedback({"planId": 1, "startTime": 100, "areaIds": [4]}, 100.0)
    events = []
    for i in range(7):
        dx = i * 0.1
        events += tracker.on_barriers(
            _clusters([(dx, 0.0), (dx + 0.2, 0.1), (dx + 0.3, 0.3)]), 101.0 + i
        )
    assert tracker.current.obstacle_count == 1
    assert len(events) == 1
    barrier = tracker.current.barriers[0]
    assert barrier.first_seen == 101.0
    assert barrier.last_seen == 107.0
    xs = [p[0] for p in barrier.points]
    assert min(xs) == 0.0
    assert max(xs) == 0.9, "the outline covers every cluster"


def test_separate_objects_stay_separate_until_a_cluster_joins_them() -> None:
    tracker = ObstacleTracker()
    tracker.on_plan_feedback({"planId": 1, "startTime": 100, "areaIds": [4]}, 100.0)
    added = tracker.on_barriers(_clusters([(0.0, 0.0), (0.3, 0.0)], [(3.0, 0.0), (3.3, 0.0)]), 101)
    assert len(added) == 2
    assert tracker.current.obstacle_count == 2
    assert tracker.on_barriers(_clusters([(0.6, 0.0), (1.5, 0.0), (2.6, 0.0)]), 102) == []
    assert tracker.current.obstacle_count == 1, "a cluster touching both makes them one"


def test_a_cluster_inside_an_outline_joins_it() -> None:
    tracker = ObstacleTracker()
    tracker.on_plan_feedback({"planId": 1, "startTime": 100, "areaIds": [4]}, 100.0)
    square = [(0.0, 0.0), (3.0, 0.0), (3.0, 3.0), (0.0, 3.0)]
    tracker.on_barriers(_clusters(square), 101)
    assert tracker.on_barriers(_clusters([(1.5, 1.5)]), 102) == []
    assert tracker.current.barriers[0].points == square


def test_runs_stored_one_barrier_per_cluster_are_grouped_on_load() -> None:
    stored = {
        "version": 2,
        "runs": [
            {
                "id": "1-100",
                "plan_id": 1,
                "area_ids": [4],
                "started": 100.0,
                "ended": 200.0,
                "barriers": [
                    {"points": [[0.0, 0.0], [0.2, 0.1]], "first_seen": 110, "last_seen": 120},
                    {"points": [[0.3, 0.1], [0.5, 0.2]], "first_seen": 115, "last_seen": 130},
                    {"points": [[6.0, 6.0]], "first_seen": 140, "last_seen": 141},
                ],
            }
        ],
    }
    run = ObstacleTracker.from_dict(stored).runs[0]
    assert run.obstacle_count == 2
    first = run.barriers[0]
    assert (first.first_seen, first.last_seen) == (110, 130)
