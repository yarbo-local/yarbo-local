"""Typed views of what the robot publishes while it works.

``plan_feedback``, ``recharge_feedback`` and ``cloud_points_feedback`` arrive at
about 2 Hz each. These are the shapes seen on firmware 3.14.11 with a Lawn Mower
Pro; every field that was not seen is absent rather than guessed. Nothing here
does I/O, and everything is frozen so a consumer can compare by equality.

Knowing the wire format is the library's job. Consumers, the Home Assistant
integration and through it the card, read these objects and never the raw dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Point = tuple[float, float]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _points(raw: Any) -> tuple[Point, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[Point] = []
    for item in raw:
        if isinstance(item, dict):
            x, y = _num(item.get("x")), _num(item.get("y"))
            if x is not None and y is not None:
                out.append((x, y))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class AreaProgress:
    """One area of the running plan: its mowing path and how far along it the robot is."""

    area_id: int | None
    clean_index: int
    clean_times: int
    path: tuple[Point, ...]

    @property
    def done(self) -> tuple[Point, ...]:
        """The part of the path already driven. ``clean_index`` counts path points."""
        return self.path[: self.clean_index + 1] if self.path else ()

    @property
    def remaining(self) -> tuple[Point, ...]:
        return self.path[self.clean_index :] if self.path else ()


@dataclass(frozen=True, slots=True)
class PlanFeedback:
    """``plan_feedback``: the plan being worked.

    Seen on the wire: it is published only while the plan is moving. It goes silent
    the moment the plan pauses or faults and starts again on resume, so its absence
    is not the end of a run. ``duration`` does not count paused time.
    """

    plan_id: int | None
    start_time: int | None
    area_ids: tuple[int, ...]
    finished_area_ids: tuple[int, ...]
    current_area_id: int | None
    state: int | None
    running_state: int | None
    total_area_m2: float | None
    finished_area_m2: float | None
    actual_area_m2: float | None
    duration_s: int | None
    remaining_s: float | None
    total_s: float | None
    battery_used: int | None
    areas: tuple[AreaProgress, ...]

    @property
    def run_id(self) -> str | None:
        """Identity of this run: the plan and when it started. Stable across pauses."""
        if self.plan_id is None or self.start_time is None:
            return None
        return f"{self.plan_id}-{self.start_time}"

    @property
    def progress(self) -> float | None:
        """Percent of the plan's area finished, 0 to 100."""
        if not self.total_area_m2 or self.finished_area_m2 is None:
            return None
        return round(min(100.0, max(0.0, 100.0 * self.finished_area_m2 / self.total_area_m2)), 1)

    @classmethod
    def from_wire(cls, value: Any) -> PlanFeedback | None:
        if not isinstance(value, dict) or "planId" not in value:
            return None
        areas = tuple(
            AreaProgress(
                area_id=_int(item.get("id")),
                clean_index=_int(item.get("clean_index")) or 0,
                clean_times=_int(item.get("clean_times")) or 0,
                path=_points(item.get("path")),
            )
            for item in value.get("cleanPathProgress") or []
            if isinstance(item, dict)
        )
        return cls(
            plan_id=_int(value.get("planId")),
            start_time=_int(value.get("startTime")),
            area_ids=tuple(
                i for i in (_int(v) for v in value.get("areaIds") or []) if i is not None
            ),
            finished_area_ids=tuple(
                i for i in (_int(v) for v in value.get("finishIds") or []) if i is not None
            ),
            current_area_id=_int(value.get("cleanAreaId")),
            state=_int(value.get("state")),
            running_state=_int(value.get("runningState")),
            total_area_m2=_num(value.get("totalCleanArea")),
            finished_area_m2=_num(value.get("finishCleanArea")),
            actual_area_m2=_num(value.get("actualCleanArea")),
            duration_s=_int(value.get("duration")),
            remaining_s=_num(value.get("leftTime")),
            total_s=_num(value.get("totalTime")),
            battery_used=_int(value.get("battery_consumption")),
            areas=areas,
        )


@dataclass(frozen=True, slots=True)
class RechargeFeedback:
    """``recharge_feedback``: the route home, published while the robot returns."""

    state: int | None
    running_state: int | None
    remaining_s: float | None
    total_s: float | None
    path: tuple[Point, ...]

    @classmethod
    def from_wire(cls, value: Any) -> RechargeFeedback | None:
        if not isinstance(value, dict) or "path" not in value:
            return None
        return cls(
            state=_int(value.get("state")),
            running_state=_int(value.get("runningState")),
            remaining_s=_num(value.get("leftTime")),
            total_s=_num(value.get("totalTime")),
            path=_points(value.get("path")),
        )


@dataclass(frozen=True, slots=True)
class BarrierPoints:
    """``cloud_points_feedback``: obstacle clusters the robot holds for the current run."""

    rotate_rad: float | None
    clusters: tuple[tuple[Point, ...], ...]

    @classmethod
    def from_wire(cls, value: Any) -> BarrierPoints | None:
        if not isinstance(value, dict) or "tmp_barrier_points" not in value:
            return None
        clusters = tuple(
            cluster
            for cluster in (_points(raw) for raw in value.get("tmp_barrier_points") or [])
            if cluster
        )
        return cls(rotate_rad=_num(value.get("rotate_rad")), clusters=clusters)
