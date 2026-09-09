"""Typed views over the robot's wire data.

Nothing here does I/O. ``RobotState`` is an immutable snapshot built by merging
``DeviceMSG`` frames and heartbeats; the session replaces it wholesale on every
change so consumers can compare by identity or equality.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from . import codec

# --- code tables (mirrors protocol/codes.yaml; kept here so models need no I/O)

PLANNING_RUNNING = frozenset({1, 2, 3, 11})
PLANNING_COMPLETED = frozenset({5, 12})
RECHARGING_IN_TRANSIT = frozenset({1, 2, 3, 99})
RTK_USABLE = frozenset({4, 5})

HEAD_TYPES = {
    0: "none",
    1: "snow_blower",
    2: "leaf_blower",
    3: "mower",
    4: "smart_cover",
    5: "mower_pro",
    99: "trimmer",
}
MOWER_HEADS = frozenset({3, 5})


class Activity(StrEnum):
    SLEEPING = "sleeping"
    IDLE = "idle"
    CALCULATING_ROUTE = "calculating_route"
    HEADING_TO_AREA = "heading_to_area"
    WORKING = "working"
    WAYPOINT = "waypoint"
    COMPLETED = "completed"
    PAUSED = "paused"
    RETURNING = "returning"
    CHARGING = "charging"
    ERROR = "error"


# --- NMEA


@dataclass(frozen=True, slots=True)
class GgaFix:
    latitude: float
    longitude: float
    quality: int
    satellites: int | None
    hdop: float | None
    altitude: float | None

    @property
    def rtk_fixed(self) -> bool:
        return self.quality == 4

    @property
    def rtk(self) -> bool:
        return self.quality in (4, 5)


def _dm_to_decimal(text: str, degree_digits: int) -> float | None:
    try:
        return float(text[:degree_digits]) + float(text[degree_digits:]) / 60.0
    except ValueError:
        return None


def parse_gga(sentence: str | None) -> GgaFix | None:
    """Parse a ``$GNGGA``/``$GPGGA`` sentence into a fix, or None."""
    if not sentence:
        return None
    body = sentence.strip()
    if body.startswith("$"):
        body = body[1:]
    star = body.rfind("*")
    if star > 0:
        body = body[:star]
    fields = body.split(",")
    if len(fields) < 10 or not fields[0].endswith("GGA"):
        return None
    lat = _dm_to_decimal(fields[2], 2) if fields[2] else None
    lon = _dm_to_decimal(fields[4], 3) if fields[4] else None
    if lat is None or lon is None:
        return None
    if fields[3] == "S":
        lat = -lat
    if fields[5] == "W":
        lon = -lon

    def _int(v: str) -> int | None:
        try:
            return int(v)
        except ValueError:
            return None

    def _float(v: str) -> float | None:
        try:
            return float(v)
        except ValueError:
            return None

    return GgaFix(
        latitude=round(lat, 8),
        longitude=round(lon, 8),
        quality=_int(fields[6]) or 0,
        satellites=_int(fields[7]),
        hdop=_float(fields[8]),
        altitude=_float(fields[9]),
    )


# --- wire envelopes


@dataclass(frozen=True, slots=True)
class Heartbeat:
    working_state: int

    @property
    def awake(self) -> bool:
        return self.working_state == 1


@dataclass(frozen=True, slots=True)
class Feedback:
    """A ``data_feedback`` envelope."""

    topic: str
    state: int
    msg: str
    data: Any

    @property
    def ok(self) -> bool:
        return self.state == 0

    @property
    def payload(self) -> Any:
        """``data`` with base64-zlib and JSON-string blobs decoded."""
        blob = codec.decode_blob(self.data)
        return blob[0] if blob is not None else self.data

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> Feedback:
        state = value.get("state", 0)
        try:
            state_int = int(state)
        except (TypeError, ValueError):
            state_int = -999
        return cls(
            topic=str(value.get("topic", "")),
            state=state_int,
            msg=str(value.get("msg", "") or ""),
            data=value.get("data"),
        )


def deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Merge ``update`` into a copy of ``base``; report whether anything changed."""
    changed = False
    out: dict[str, Any] = dict(base)
    for key, value in update.items():
        current = out.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            merged, sub_changed = deep_merge(current, value)
            if sub_changed:
                out[key] = merged
                changed = True
        elif current != value or key not in out:
            out[key] = value
            changed = True
    return out, changed


# --- robot state


def _get(raw: Mapping[str, Any], path: str) -> Any:
    node: Any = raw
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RobotState:
    """Immutable snapshot of everything known about the robot."""

    raw: Mapping[str, Any] = field(default_factory=dict)
    awake: bool | None = None
    serial: str | None = None
    last_frame_at: float | None = None
    last_heartbeat_at: float | None = None

    # -- construction

    def with_frame(self, frame: Mapping[str, Any], at: float) -> tuple[RobotState, bool]:
        merged, changed = deep_merge(self.raw, frame)
        if not changed:
            return replace(self, last_frame_at=at), False
        return replace(self, raw=merged, last_frame_at=at), True

    def with_heartbeat(self, hb: Heartbeat, at: float) -> tuple[RobotState, bool]:
        changed = self.awake != hb.awake
        return replace(self, awake=hb.awake, last_heartbeat_at=at), changed

    # -- raw access

    def get(self, path: str) -> Any:
        return _get(self.raw, path)

    # -- core fields

    @property
    def firmware(self) -> str | None:
        value = self.get("version")
        return str(value) if value else None

    @property
    def body_firmware(self) -> str | None:
        value = self.get("BodyVersionMsg.body_fw_version")
        return str(value) if value else None

    @property
    def head_firmware(self) -> str | None:
        value = self.get("HeadAndVersionCheck.head_firmware_version")
        return str(value) if value and value != "0.0.0" else None

    @property
    def battery(self) -> int | None:
        return _int_or_none(self.get("BatteryMSG.capacity"))

    @property
    def battery_health(self) -> int | None:
        return _int_or_none(self.get("BatteryMSG.health"))

    @property
    def battery_current(self) -> float | None:
        """Amps. Negative while discharging (-0.3 A at rest); charging sign not yet captured."""
        raw = _int_or_none(self.get("BatteryMSG.current"))
        return raw / 1000.0 if raw is not None else None

    @property
    def battery_voltage(self) -> float | None:
        raw = _int_or_none(self.get("BatteryMSG.voltage"))
        return raw / 1000.0 if raw is not None else None

    @property
    def charging(self) -> bool:
        """Candidate rule: ``BatteryMSG.status`` above 1 or ``StateMSG.charging_status`` set.

        ``BodyMsg.recharge_state`` is not used: it read 3 while the robot sat off any
        charger and discharged, so the community's 'wired charging' mapping is wrong.
        No charging capture exists yet, so this is unverified in the positive direction.
        """
        status = _int_or_none(self.get("BatteryMSG.status")) or 0
        charging_status = _int_or_none(self.get("StateMSG.charging_status")) or 0
        return status > 1 or charging_status > 0

    @property
    def error_code(self) -> int:
        return _int_or_none(self.get("StateMSG.error_code")) or 0

    @property
    def planning_code(self) -> int:
        return _int_or_none(self.get("StateMSG.on_going_planning")) or 0

    @property
    def paused_code(self) -> int:
        return _int_or_none(self.get("StateMSG.planning_paused")) or 0

    @property
    def recharging_code(self) -> int:
        return _int_or_none(self.get("StateMSG.on_going_recharging")) or 0

    @property
    def plan_running(self) -> bool:
        return self.planning_code in PLANNING_RUNNING

    @property
    def returning(self) -> bool:
        return self.recharging_code in RECHARGING_IN_TRANSIT

    @property
    def head_type(self) -> int | None:
        return _int_or_none(self.get("HeadMsg.head_type"))

    @property
    def head_name(self) -> str:
        return HEAD_TYPES.get(self.head_type if self.head_type is not None else -1, "unknown")

    @property
    def has_mower_head(self) -> bool:
        return self.head_type in MOWER_HEADS

    @property
    def rtk_status(self) -> int | None:
        return _int_or_none(self.get("RTKMSG.status"))

    @property
    def rtk_usable(self) -> bool:
        return self.rtk_status in RTK_USABLE

    @property
    def satellites(self) -> int | None:
        return _int_or_none(self.get("RTKMSG.sat_num"))

    @property
    def heading_degrees(self) -> float | None:
        return _float_or_none(self.get("RTKMSG.heading"))

    @property
    def position(self) -> tuple[float, float, float] | None:
        x = _float_or_none(self.get("CombinedOdom.x"))
        y = _float_or_none(self.get("CombinedOdom.y"))
        phi = _float_or_none(self.get("CombinedOdom.phi"))
        if x is None or y is None:
            return None
        return (x, y, phi if phi is not None else 0.0)

    @property
    def fix(self) -> GgaFix | None:
        return parse_gga(self.get("rtk_base_data.rover.gngga"))

    @property
    def volume(self) -> float | None:
        return _float_or_none(self.get("StateMSG.volume"))

    @property
    def sound_enabled(self) -> bool | None:
        value = self.get("StateMSG.enable_sound")
        return bool(value) if value is not None else None

    @property
    def person_detection(self) -> bool | None:
        value = self.get("StateMSG.person_detect_status")
        return bool(value) if value is not None else None

    @property
    def child_lock(self) -> bool | None:
        value = self.get("StateMSG.child_lock_status")
        return bool(value) if value is not None else None

    @property
    def follow_mode(self) -> bool | None:
        value = self.get("StateMSG.robot_follow_state")
        return bool(value) if value is not None else None

    @property
    def ambient_temperature(self) -> float | None:
        return _float_or_none(self.get("EletricMSG.body_ambient_ntc_temp"))

    @property
    def halow_rssi(self) -> int | None:
        return _int_or_none(self.get("halow_status.strength"))

    @property
    def network_path(self) -> str | None:
        """Interface with the lowest non-negative route metric: halow, wifi or lte."""
        prio = self.get("route_priority")
        if not isinstance(prio, Mapping):
            return None
        names = {"hg0": "halow", "wlan0": "wifi", "wwan0": "lte"}
        best: tuple[int, str] | None = None
        for iface, label in names.items():
            metric = _int_or_none(prio.get(iface))
            if metric is None or metric < 0:
                continue
            if best is None or metric < best[0]:
                best = (metric, label)
        return best[1] if best else None

    # -- derived

    @property
    def activity(self) -> Activity:  # noqa: PLR0911 - one return per state, by design
        if self.error_code != 0:
            return Activity.ERROR
        p = self.planning_code
        if p in PLANNING_RUNNING and self.paused_code > 0:
            return Activity.PAUSED
        if p == 2:
            return Activity.CALCULATING_ROUTE
        if p == 3:
            return Activity.HEADING_TO_AREA
        if p == 1:
            return Activity.WORKING
        if p == 11:
            return Activity.WAYPOINT
        if p in PLANNING_COMPLETED:
            return Activity.COMPLETED
        if self.returning:
            return Activity.RETURNING
        if self.charging:
            return Activity.CHARGING
        if self.awake is False:
            return Activity.SLEEPING
        return Activity.IDLE


# --- map


@dataclass(frozen=True, slots=True)
class Zone:
    family: str
    id: int | None
    name: str
    type: int | None
    enabled: bool
    points: tuple[tuple[float, float, float], ...]
    ref: tuple[float, float] | None
    extra: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ChargingPoint:
    id: int | None
    name: str
    point: tuple[float, float, float]
    start_point: tuple[float, float, float] | None
    straight_phi: float | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class SiteMap:
    zones: tuple[Zone, ...]
    charging: tuple[ChargingPoint, ...]
    reference: tuple[float, float] | None
    raw: Mapping[str, Any]

    def family(self, name: str) -> list[Zone]:
        return [z for z in self.zones if z.family == name]


ZONE_FAMILIES = (
    "areas",
    "nogozones",
    "novisionzones",
    "elec_fence",
    "pathways",
    "sidewalks",
    "deadends",
)


def _point(raw: Any) -> tuple[float, float, float] | None:
    if not isinstance(raw, Mapping):
        return None
    x = _float_or_none(raw.get("x"))
    y = _float_or_none(raw.get("y"))
    if x is None or y is None:
        return None
    return (x, y, _float_or_none(raw.get("phi")) or 0.0)


def _ref(raw: Any) -> tuple[float, float] | None:
    if not isinstance(raw, Mapping):
        return None
    lat = _float_or_none(raw.get("latitude"))
    lon = _float_or_none(raw.get("longitude"))
    if lat is None or lon is None or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
        return None
    return (lat, lon)


def parse_site_map(obj: Mapping[str, Any]) -> SiteMap:
    """Build a SiteMap from a decoded ``get_map`` payload."""
    zones: list[Zone] = []
    reference: tuple[float, float] | None = None
    for family in ZONE_FAMILIES:
        for record in obj.get(family) or []:
            if not isinstance(record, Mapping):
                continue
            points = tuple(p for p in (_point(r) for r in record.get("range") or []) if p)
            ref = _ref(record.get("ref"))
            reference = reference or ref
            extra = {
                k: v
                for k, v in record.items()
                if k not in ("id", "name", "type", "enable", "range", "ref")
            }
            zones.append(
                Zone(
                    family=family,
                    id=_int_or_none(record.get("id")),
                    name=str(record.get("name") or ""),
                    type=_int_or_none(record.get("type")),
                    enabled=bool(record.get("enable", True)),
                    points=points,
                    ref=ref,
                    extra=extra,
                )
            )
    charging: list[ChargingPoint] = []
    for record in obj.get("allchargingData") or []:
        if not isinstance(record, Mapping):
            continue
        point = _point(record.get("chargingPoint"))
        if point is None:
            continue
        charging.append(
            ChargingPoint(
                id=_int_or_none(record.get("id")),
                name=str(record.get("name") or ""),
                point=point,
                start_point=_point(record.get("startPoint")),
                straight_phi=_float_or_none(record.get("straightPhi")),
                enabled=bool(record.get("enable", True)),
            )
        )
    return SiteMap(zones=tuple(zones), charging=tuple(charging), reference=reference, raw=obj)


# --- plans and references


@dataclass(frozen=True, slots=True)
class PlanSummary:
    id: int
    name: str
    area_ids: tuple[int, ...]
    self_order: bool | None


def parse_plans(payload: Any) -> list[PlanSummary]:
    """Accept ``{"data": [...]}``, a bare list, or None."""
    items: Any = payload
    if isinstance(items, Mapping):
        items = items.get("data")
    if not isinstance(items, list):
        return []
    plans: list[PlanSummary] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        plan_id = _int_or_none(item.get("id"))
        if plan_id is None:
            continue
        areas = item.get("areaIds") or []
        plans.append(
            PlanSummary(
                id=plan_id,
                name=str(item.get("name") or ""),
                area_ids=tuple(a for a in (_int_or_none(x) for x in areas) if a is not None),
                self_order=(
                    bool(item["enable_self_order"]) if "enable_self_order" in item else None
                ),
            )
        )
    return plans


@dataclass(frozen=True, slots=True)
class GpsReference:
    latitude: float
    longitude: float
    height: float | None
    fix_type: int | None

    @property
    def fixed(self) -> bool:
        return self.fix_type == 1


def parse_gps_ref(payload: Any) -> GpsReference | None:
    if not isinstance(payload, Mapping):
        return None
    ref = _ref(payload.get("ref"))
    if ref is None:
        return None
    return GpsReference(
        latitude=ref[0],
        longitude=ref[1],
        height=_float_or_none(payload.get("hgt")),
        fix_type=_int_or_none(payload.get("rtkFixType")),
    )
