"""Building the payloads of the commands that change something on the robot.

Pure functions, no I/O. Every shape here was read from the app or verified on a real
robot (firmware 3.14.11); the fixtures named in ``protocol/commands.yaml`` are the
evidence. Where the robot stores a whole record, the app sends the whole record back
with one value changed, and so do these helpers: start from what the robot returned,
keep the keys the app sends, change only what was asked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# A point of a zone or a line: metres from the zone's ``ref``, x west and y north, with an
# optional heading. The robot stores {"x", "y", "phi"}.
PointLike = Sequence[float]
RefLike = tuple[float, float]

# The 56 keys the app sends with ``save_global_params``, of the 68 ``read_global_params``
# returns (fixtures verify-settings-saves and app-plan-schedule-settings-rain).
APP_GLOBAL_PARAM_KEYS: tuple[str, ...] = (
    "adaptive_resume",
    "anti_freeze_ctl",
    "area_max_low_cfd_dis",
    "auto_linght_mode_inschdule",
    "chute_max_angle",
    "chute_min_angle",
    "clean_mode",
    "close_roller_battery",
    "default_plan_id",
    "dijkstra_path_enable",
    "docking_station_clean_times",
    "docking_station_snow_throw_dir",
    "docking_station_turn_mode",
    "draw_path_max_low_cfd_dis",
    "en_trimmer",
    "enable_advanced_fusion",
    "enable_blower_bypass",
    "enable_elec_fence",
    "enable_no_notice",
    "enable_sam_bypass",
    "enable_snowbot_bypass",
    "enable_video_record",
    "enable_weather_schedule",
    "id",
    "lift_detec",
    "max_rev",
    "max_roller_speed",
    "min_rev",
    "min_roller_speed",
    "new_low_battery_shutdown_enable",
    "path_max_low_cfd_dis",
    "pitch_max_angle",
    "pitch_min_angle",
    "plan_roller_stop",
    "plan_speed",
    "push_pod_max_place",
    "push_pod_min_place",
    "rain_temp_ntc_sensor_detec",
    "rain_trigger_23_threshold",
    "rain_trigger_24_threshold",
    "recharge_battery",
    "recharge_battery_max",
    "recharge_battery_min",
    "resume_battery",
    "resume_battery_max",
    "resume_battery_min",
    "rollover_tilt_detec",
    "slope_compensate_switch",
    "stable_roller_current",
    "standby_time",
    "stop_roller_speed",
    "tow_obstacle_detect_status",
    "tow_person_detect_status",
    "trimmer_blade_speed",
    "trimmer_line_feed_frequency",
    "trimming_moving_speed",
)
GLOBAL_PARAMS_ID = 1

# The 31 keys the app sends with ``save_mower_area_params``. ``read_mower_area_params``
# returns 30 of them, plus three the app leaves out; ``mapping_module_type`` is not
# returned and the app always sends 99.
APP_MOWER_AREA_KEYS: tuple[str, ...] = (
    "auto_step_angle",
    "avoidance_level",
    "buffer_dis",
    "clean_times",
    "contract_dis",
    "current_index",
    "double_clean_params",
    "edge_area_first",
    "edge_bypass_sw",
    "edge_circles",
    "edge_direction_mode",
    "en_trimmer",
    "enable_clean_times_dis",
    "enable_explore",
    "first_clean_params",
    "gap",
    "id",
    "m1pro_sensor_switch",
    "mapping_module_type",
    "move2area_max_offset",
    "move2area_offset_enable",
    "move2area_step_offset",
    "mower_ngz_edge_sw",
    "mower_target_height_count",
    "ngz_expand_dis",
    "parallel_angle_max",
    "parallel_angle_min",
    "parallel_offset_mode",
    "route_order",
    "sensor_switch",
    "side_enable",
)
MAPPING_MODULE_TYPE = 99

# The keys the app sends with ``save_clean_area`` when it edits an area in place.
APP_AREA_KEYS: tuple[str, ...] = (
    "id",
    "name",
    "algorithm_type",
    "labels",
    "leaf_piles",
    "push_snow_dir",
    "range",
    "ref",
    "slope_degrees",
    "snowPiles",
    "trimming_edges",
)


def points(raw: Iterable[PointLike]) -> list[dict[str, float]]:
    """``[(x, y), ...]`` or ``[(x, y, phi), ...]`` as the robot's ``range`` list."""
    out: list[dict[str, float]] = []
    for point in raw:
        if len(point) not in (2, 3):
            raise ValueError(f"a point is (x, y) or (x, y, phi), not {point!r}")
        phi = float(point[2]) if len(point) == 3 else 0.0
        out.append({"x": float(point[0]), "y": float(point[1]), "phi": phi})
    return out


def ref(value: RefLike) -> dict[str, float]:
    """A ``(latitude, longitude)`` reference as the robot stores it."""
    latitude, longitude = value
    return {"latitude": float(latitude), "longitude": float(longitude)}


def changed_record(
    current: Mapping[str, Any],
    keys: Sequence[str],
    changes: Mapping[str, Any],
    *,
    fixed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The whole record to send back: ``current`` cut to ``keys``, with ``changes`` applied.

    A change to a key the app never sends is refused, as is a change to a ``fixed`` key.
    A change given as a mapping updates the stored mapping instead of replacing it, so
    ``first_clean_params={"blade_height": 60}`` keeps the other six values.
    """
    fixed = dict(fixed or {})
    unknown = sorted(k for k in changes if k not in keys or k in fixed)
    if unknown:
        raise ValueError(f"not a setting the app sends: {', '.join(unknown)}")
    record: dict[str, Any] = {k: current[k] for k in keys if k in current}
    for key, value in changes.items():
        stored = record.get(key)
        if isinstance(value, Mapping) and isinstance(stored, Mapping):
            record[key] = {**stored, **value}
        else:
            record[key] = value
    record.update(fixed)
    return record


def plan(
    name: str, area_ids: Sequence[int], *, plan_id: int | None, self_order: bool
) -> dict[str, Any]:
    if not name.strip():
        raise ValueError("a plan needs a name")
    if not area_ids:
        raise ValueError("a plan needs at least one area")
    body: dict[str, Any] = {
        "areaIds": [int(a) for a in area_ids],
        "name": name,
        "enable_self_order": bool(self_order),
    }
    if plan_id is not None:
        body = {"id": int(plan_id), **body}
    return body


def _clock(value: str, what: str) -> str:
    parts = value.split(":")
    if len(parts) != 3 or not all(p.isdigit() and len(p) == 2 for p in parts):
        raise ValueError(f"{what} must be HH:MM:SS, not {value!r}")
    hours, minutes, seconds = (int(p) for p in parts)
    if hours > 23 or minutes > 59 or seconds > 59:
        raise ValueError(f"{what} must be a time of day, not {value!r}")
    return value


def schedule(
    *,
    schedule_id: int,
    plan_id: int,
    name: str,
    start_time: str,
    end_time: str,
    week_day: int,
    schedule_type: int,
    enabled: bool,
    return_method: int,
    resume_progress: bool,
    last_progress: int,
) -> dict[str, Any]:
    """The eleven keys the app sent with ``save_schedule``."""
    return {
        "id": int(schedule_id),
        "plan_id": int(plan_id),
        "start_time": _clock(start_time, "start_time"),
        "enable": bool(enabled),
        "schedule_type": int(schedule_type),
        "enable_last_progress": bool(resume_progress),
        "last_progress": int(last_progress),
        "name": name,
        "week_day": int(week_day),
        "end_time": _clock(end_time, "end_time"),
        "return_method": int(return_method),
    }


def sound(*, enabled: bool, volume: float) -> dict[str, Any]:
    if not 0.0 <= volume <= 1.0:
        raise ValueError("volume is 0.0 to 1.0")
    return {"enable": bool(enabled), "vol": float(volume), "mode": 0}


def _with_id(body: dict[str, Any], object_id: int | None) -> dict[str, Any]:
    return body if object_id is None else {"id": int(object_id), **body}


def nogozone(
    name: str,
    outline: Iterable[PointLike],
    reference: RefLike,
    *,
    zone_id: int | None,
    enabled: bool,
) -> dict[str, Any]:
    corners = points(outline)
    if len(corners) < 3:
        raise ValueError("a no-go zone needs at least three points")
    body = {
        "name": name,
        "type": 0,
        "enable": bool(enabled),
        "range": corners,
        "ref": ref(reference),
        "trimming_edges": [],
    }
    return _with_id(body, zone_id)


def pathway(
    name: str, line: Iterable[PointLike], reference: RefLike, *, pathway_id: int | None
) -> dict[str, Any]:
    route = points(line)
    if len(route) < 2:
        raise ValueError("a pathway needs at least two points")
    body = {
        "connectids": [],
        "leaf_piles": [],
        "name": name,
        "range": route,
        "ref": ref(reference),
        "snowPiles": [],
        "trimming_edges": [],
    }
    return _with_id(body, pathway_id)


def sidewalk(
    name: str, line: Iterable[PointLike], reference: RefLike, *, sidewalk_id: int | None
) -> dict[str, Any]:
    route = points(line)
    if len(route) < 2:
        raise ValueError("a sidewalk needs at least two points")
    body = {
        "name": name,
        "head_type": 99,
        "range": route,
        "ref": ref(reference),
        "connectids": [],
        "snowPiles": [],
        "trimming_edges": [],
    }
    return _with_id(body, sidewalk_id)
