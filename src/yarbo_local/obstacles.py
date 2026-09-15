"""Obstacle log across plan runs. Pure logic, no I/O.

The only obstacle source is the robot's own report, ``cloud_points_feedback.tmp_barrier_points``:
clusters of ``{x, y}`` in the map frame. During the East Lawn run on 3.14.11 it held 18
distinct obstacles.

The front ultrasonic distances (``ultrasonic_msg``) are deliberately not used. Logging their
close readings as obstacles on 2026-09-15 produced a new "obstacle" every few seconds in a
line beside the robot while it mowed: the sensor sees the uncut grass next to the strip.
The user confirmed none of them existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from .models import RobotState

BARRIER_MERGE_M = 0.35
MAX_BARRIERS_PER_RUN = 2000
MAX_RUNS = 30


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


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
    barriers: list[Barrier] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.ended is None

    @property
    def obstacle_count(self) -> int:
        return len(self.barriers)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "plan_name": self.plan_name,
            "area_ids": self.area_ids,
            "started": _r(self.started, 1),
            "ended": _r(self.ended, 1) if self.ended is not None else None,
            "barriers": len(self.barriers),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary(), "barriers": [b.to_dict() for b in self.barriers]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Run:
        # Stored runs from 0.1.x may carry "detections" from ultrasonic readings; they were
        # grass, not obstacles, and are dropped here.
        return cls(
            id=str(d["id"]),
            plan_id=d.get("plan_id"),
            area_ids=[int(a) for a in d.get("area_ids") or []],
            started=float(d.get("started", 0.0)),
            ended=float(d["ended"]) if d.get("ended") is not None else None,
            plan_name=d.get("plan_name"),
            barriers=[Barrier.from_dict(x) for x in d.get("barriers") or []],
        )


class ObstacleTracker:
    """Keeps the obstacle log for recent plan runs, newest last."""

    def __init__(self, runs: list[Run] | None = None, *, max_runs: int = MAX_RUNS) -> None:
        self.runs: list[Run] = list(runs or [])
        self.max_runs = max_runs

    def to_dict(self) -> dict[str, Any]:
        return {"version": 2, "runs": [r.to_dict() for r in self.runs]}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ObstacleTracker:
        return cls([Run.from_dict(r) for r in (data or {}).get("runs") or []])

    @staticmethod
    def needs_migration(data: dict[str, Any] | None) -> bool:
        """True when stored data still holds the discarded ultrasonic detections."""
        return any("detections" in r for r in (data or {}).get("runs") or [])

    @property
    def current(self) -> Run | None:
        return self.runs[-1] if self.runs and self.runs[-1].active else None

    @property
    def latest(self) -> Run | None:
        return self.runs[-1] if self.runs else None

    def get(self, run_id: str) -> Run | None:
        return next((r for r in self.runs if r.id == run_id), None)

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
        raw_areas = value.get("areaIds")
        area_ids: list[Any] = raw_areas if isinstance(raw_areas, list) else []
        self.runs.append(
            Run(
                id=run_id,
                plan_id=value.get("planId"),
                area_ids=[int(a) for a in area_ids if isinstance(a, int)],
                started=float(start) if isinstance(start, int | float) else t,
            )
        )
        if len(self.runs) > self.max_runs:
            self.runs = self.runs[-self.max_runs :]
        return True

    def on_barriers(self, value: Any, t: float) -> list[Barrier]:
        """Merge the robot's reported obstacle clusters into the current run; return new ones."""
        run = self.current
        clusters = value.get("tmp_barrier_points") if isinstance(value, dict) else None
        if run is None or not isinstance(clusters, list):
            return []
        added: list[Barrier] = []
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
                (b for b in run.barriers if math.dist(b.centre, centre) < BARRIER_MERGE_M), None
            )
            if match is not None:
                match.last_seen = t
                continue
            if len(run.barriers) >= MAX_BARRIERS_PER_RUN:
                break
            run.barriers.append(barrier)
            added.append(barrier)
        return added

    def on_state(self, state: RobotState, t: float) -> bool:
        """Close the run when the robot is neither planning nor returning. True when it closed."""
        run = self.current
        if run is not None and state.planning_code == 0 and state.recharging_code == 0:
            run.ended = t
            return True
        return False
