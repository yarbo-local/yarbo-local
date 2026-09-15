"""Obstacle tracking across plan runs. Pure logic, no I/O.

Two sources, verified on firmware 3.14.11 with a Lawn Mower Pro:

- ``cloud_points_feedback.tmp_barrier_points``: clusters of ``{x, y}`` in the map frame.
  During one run it held 18 distinct obstacles; during another it stayed empty while
  the robot visibly avoided things.
- ``ultrasonic_msg.lf_dis`` / ``mt_dis`` / ``rf_dis``: front-left, middle and front-right
  distances. Readings between about 0.2 m and 2 m appear as the robot closes on
  something and it reverses shortly after; ``-3``, ``0`` and ``9999`` mean no echo.
  The unit is taken to be millimetres from the observed range.

An ultrasonic *episode* is a run of valid readings on one sensor with gaps shorter than
``EPISODE_GAP_S``. Each episode becomes one detection at its closest reading, placed on
the map from the robot pose and an estimated sensor mounting. Detections within
``MERGE_M`` of an earlier one in the same run count as the same obstacle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from .models import RobotState

ULTRASONIC_FIELDS = {
    "left": "ultrasonic_msg.lf_dis",
    "middle": "ultrasonic_msg.mt_dis",
    "right": "ultrasonic_msg.rf_dis",
}
VALID_MM = (50, 4000)
# Estimated mounting in the body frame (x forward, y left): metres, metres, outward angle.
# Not measured on the robot; the placed point is an estimate and is labelled as one.
SENSOR_MOUNT = {
    "left": (0.60, 0.22, math.radians(20)),
    "middle": (0.70, 0.0, 0.0),
    "right": (0.60, -0.22, math.radians(-20)),
}
EPISODE_GAP_S = 2.0
MERGE_M = 0.5
BARRIER_MERGE_M = 0.35
MAX_ITEMS_PER_RUN = 2000
MAX_RUNS = 30


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


@dataclass
class Detection:
    t: float
    source: str
    distance_m: float
    robot: tuple[float, float, float]
    point: tuple[float, float]
    count: int = 1
    last_t: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": _r(self.t, 1),
            "last_t": _r(self.last_t or self.t, 1),
            "source": self.source,
            "distance_m": _r(self.distance_m, 2),
            "robot": [_r(self.robot[0], 2), _r(self.robot[1], 2), _r(self.robot[2], 3)],
            "point": [_r(self.point[0], 2), _r(self.point[1], 2)],
            "count": self.count,
            "estimated": True,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Detection:
        robot = d.get("robot") or [0.0, 0.0, 0.0]
        point = d.get("point") or [0.0, 0.0]
        return cls(
            t=float(d["t"]),
            source=str(d.get("source", "")),
            distance_m=float(d.get("distance_m", 0.0)),
            robot=(float(robot[0]), float(robot[1]), float(robot[2])),
            point=(float(point[0]), float(point[1])),
            count=int(d.get("count", 1)),
            last_t=float(d.get("last_t", d["t"])),
        )


@dataclass
class Barrier:
    points: list[tuple[float, float]]
    first_seen: float
    last_seen: float

    @property
    def centre(self) -> tuple[float, float]:
        return (
            sum(p[0] for p in self.points) / len(self.points),
            sum(p[1] for p in self.points) / len(self.points),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "points": [[_r(x, 2), _r(y, 2)] for x, y in self.points],
            "first_seen": _r(self.first_seen, 1),
            "last_seen": _r(self.last_seen, 1),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Barrier:
        return cls(
            points=[(float(p[0]), float(p[1])) for p in d.get("points") or []],
            first_seen=float(d.get("first_seen", 0.0)),
            last_seen=float(d.get("last_seen", 0.0)),
        )


@dataclass
class Run:
    id: str
    plan_id: int | None
    area_ids: list[int]
    started: float
    ended: float | None = None
    plan_name: str | None = None
    detections: list[Detection] = field(default_factory=list)
    barriers: list[Barrier] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.ended is None

    @property
    def obstacle_count(self) -> int:
        return len(self.detections) + len(self.barriers)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "plan_name": self.plan_name,
            "area_ids": self.area_ids,
            "started": _r(self.started, 1),
            "ended": _r(self.ended, 1) if self.ended is not None else None,
            "detections": len(self.detections),
            "barriers": len(self.barriers),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "detections": [d.to_dict() for d in self.detections],
            "barriers": [b.to_dict() for b in self.barriers],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Run:
        return cls(
            id=str(d["id"]),
            plan_id=d.get("plan_id"),
            area_ids=[int(a) for a in d.get("area_ids") or []],
            started=float(d.get("started", 0.0)),
            ended=float(d["ended"]) if d.get("ended") is not None else None,
            plan_name=d.get("plan_name"),
            detections=[Detection.from_dict(x) for x in d.get("detections") or []],
            barriers=[Barrier.from_dict(x) for x in d.get("barriers") or []],
        )


@dataclass
class _Episode:
    started: float
    last_valid: float
    distance_m: float
    robot: tuple[float, float, float] | None


class ObstacleTracker:
    """Keeps the obstacle log for recent plan runs, newest last."""

    def __init__(self, runs: list[Run] | None = None, *, max_runs: int = MAX_RUNS) -> None:
        self.runs: list[Run] = list(runs or [])
        self.max_runs = max_runs
        self._episodes: dict[str, _Episode] = {}

    # -- persistence

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "runs": [r.to_dict() for r in self.runs]}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ObstacleTracker:
        runs = [Run.from_dict(r) for r in (data or {}).get("runs") or []]
        # A run left open by a restart is closed at its last evidence; a later
        # plan_feedback with the same id reopens it.
        return cls(runs)

    # -- queries

    @property
    def current(self) -> Run | None:
        return self.runs[-1] if self.runs and self.runs[-1].active else None

    @property
    def latest(self) -> Run | None:
        return self.runs[-1] if self.runs else None

    def get(self, run_id: str) -> Run | None:
        return next((r for r in self.runs if r.id == run_id), None)

    # -- inputs

    def on_plan_feedback(self, value: Any, t: float) -> bool:
        """Start or reopen the run named by ``(planId, startTime)``.

        Returns True when that changed the log.
        """
        if not isinstance(value, dict) or value.get("planId") is None:
            return False
        start = value.get("startTime")
        run_id = f"{value.get('planId')}-{int(start) if isinstance(start, int | float) else start}"
        latest = self.latest
        if latest is not None and latest.id == run_id:
            if latest.ended is not None:
                latest.ended = None
                return True
            return False
        if latest is not None and latest.active:
            latest.ended = t
        started = value.get("startTime")
        raw_areas = value.get("areaIds")
        area_ids: list[Any] = raw_areas if isinstance(raw_areas, list) else []
        self.runs.append(
            Run(
                id=run_id,
                plan_id=value.get("planId"),
                area_ids=[int(a) for a in area_ids if isinstance(a, int)],
                started=float(started) if isinstance(started, int | float) else t,
            )
        )
        self._episodes.clear()
        if len(self.runs) > self.max_runs:
            self.runs = self.runs[-self.max_runs :]
        return True

    def on_barriers(self, value: Any, t: float) -> list[Barrier]:
        run = self.current
        clusters = value.get("tmp_barrier_points") if isinstance(value, dict) else None
        if run is None or not isinstance(clusters, list):
            return []
        added: list[Barrier] = []
        centres = [b.centre for b in run.barriers]
        for raw in clusters:
            items = raw if isinstance(raw, list) else [raw]
            points = [
                (float(p["x"]), float(p["y"]))
                for p in items
                if isinstance(p, dict)
                and isinstance(p.get("x"), int | float)
                and isinstance(p.get("y"), int | float)
            ]
            if not points:
                continue
            barrier = Barrier(points=points, first_seen=t, last_seen=t)
            centre = barrier.centre
            match = next(
                (
                    b
                    for b, c in zip(run.barriers, centres, strict=True)
                    if math.dist(c, centre) < BARRIER_MERGE_M
                ),
                None,
            )
            if match is not None:
                match.last_seen = t
                continue
            if run.obstacle_count >= MAX_ITEMS_PER_RUN:
                break
            run.barriers.append(barrier)
            centres.append(centre)
            added.append(barrier)
        return added

    def on_state(self, state: RobotState, t: float) -> list[Detection]:
        """Feed every telemetry frame. Returns new obstacles detected by ultrasonic sensors."""
        run = self.current
        if run is not None and state.planning_code == 0 and state.recharging_code == 0:
            closing = self._close_all(run, t)
            run.ended = t
            return closing
        if run is None:
            self._episodes.clear()
            return []
        pose = state.position
        new: list[Detection] = []
        for source, path in ULTRASONIC_FIELDS.items():
            raw = state.get(path)
            valid = (
                isinstance(raw, int | float)
                and not isinstance(raw, bool)
                and VALID_MM[0] <= raw <= VALID_MM[1]
            )
            episode = self._episodes.get(source)
            if episode is not None and t - episode.last_valid > EPISODE_GAP_S:
                detection = self._close(run, source, episode)
                if detection is not None:
                    new.append(detection)
                episode = None
            if not valid:
                continue
            distance = float(raw) / 1000.0
            if episode is None:
                episode = self._episodes[source] = _Episode(t, t, distance, pose)
            episode.last_valid = t
            if distance <= episode.distance_m or episode.robot is None:
                episode.distance_m = distance
                episode.robot = pose if pose is not None else episode.robot
        return new

    # -- internals

    def _close_all(self, run: Run, t: float) -> list[Detection]:
        new = []
        for source, episode in list(self._episodes.items()):
            detection = self._close(run, source, episode)
            if detection is not None:
                new.append(detection)
        return new

    def _close(self, run: Run, source: str, episode: _Episode) -> Detection | None:
        self._episodes.pop(source, None)
        if episode.robot is None:
            return None
        point = estimate_point(source, episode.distance_m, episode.robot)
        for existing in run.detections:
            if math.dist(existing.point, point) < MERGE_M:
                existing.count += 1
                existing.last_t = episode.last_valid
                existing.distance_m = min(existing.distance_m, episode.distance_m)
                return None
        if run.obstacle_count >= MAX_ITEMS_PER_RUN:
            return None
        detection = Detection(
            t=episode.started,
            source=f"ultrasonic_{source}",
            distance_m=episode.distance_m,
            robot=episode.robot,
            point=point,
            last_t=episode.last_valid,
        )
        run.detections.append(detection)
        return detection


def estimate_point(
    source: str, distance_m: float, robot: tuple[float, float, float]
) -> tuple[float, float]:
    """Where an ultrasonic echo came from, in the map frame.

    An estimate: the sensor mounting is not measured.
    """
    mx, my, angle = SENSOR_MOUNT.get(source, SENSOR_MOUNT["middle"])
    bx = mx + distance_m * math.cos(angle)
    by = my + distance_m * math.sin(angle)
    x, y, phi = robot
    c, s = math.cos(phi), math.sin(phi)
    return (x + bx * c - by * s, y + bx * s + by * c)
