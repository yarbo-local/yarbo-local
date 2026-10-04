"""A robot simulator built from fixtures.

It answers the verified read commands from a captured snapshot, honours the
wake command by starting to "stream", and models the auto-sleep window. It
runs either against a :class:`FakeTransport` (tests) or, through
``yarbo-local sim``, against any MQTT broker so contributors without a robot
can develop the integration.
"""

from __future__ import annotations

import asyncio
import base64
import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any
import zlib

from . import codec, topics
from .transport import FakeTransport, Message, Transport

SLEEP_AFTER = 200.0


def load_site_map(fixture: Path) -> dict[str, Any]:
    """The decoded ``get_map`` data from a fixture (the last reply in the file)."""
    found: dict[str, Any] | None = None
    for line in fixture.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        payload = rec.get("payload")
        if isinstance(payload, dict) and payload.get("topic") == "get_map":
            blob = codec.decode_blob(payload.get("data"))
            if blob is not None and isinstance(blob[0], dict):
                found = blob[0]
            elif isinstance(payload.get("data"), dict):
                found = payload["data"]
    if found is None:
        raise ValueError(f"{fixture}: no get_map reply found")
    return found


def load_snapshot(fixture: Path) -> tuple[str, dict[str, Any]]:
    """Return ``(serial, snapshot)`` from a get_device_msg fixture."""
    serial: str | None = None
    snapshot: dict[str, Any] | None = None
    for line in fixture.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        parsed = topics.parse(str(rec.get("topic", "")))
        if parsed is None:
            continue
        serial = serial or parsed.serial
        payload = rec.get("payload")
        if (
            parsed.leaf == "data_feedback"
            and isinstance(payload, dict)
            and payload.get("topic") == "get_device_msg"
            and isinstance(payload.get("data"), dict)
        ):
            snapshot = payload["data"]
    if serial is None or snapshot is None:
        raise ValueError(f"{fixture}: no get_device_msg snapshot found")
    return serial, snapshot


@dataclass
class Simulator:
    """Minimal behavioural model of one robot."""

    serial: str
    snapshot: dict[str, Any]
    firmware: str = "3.14.11"
    plans: list[dict[str, Any]] = field(default_factory=list)
    site_map: dict[str, Any] = field(default_factory=dict)
    awake: bool = False
    controller_holder: str | None = None
    woke_at: float | None = None
    now: Any = time.monotonic
    _transport: Transport | None = None
    log: list[tuple[str, Any]] = field(default_factory=list)
    schedules: list[dict[str, Any]] = field(default_factory=list)
    global_params: dict[str, Any] = field(default_factory=dict)
    mower_area_params: dict[int, dict[str, Any]] = field(default_factory=dict)
    # No-go zones, pathways and sidewalks are numbered from one shared counter.
    next_object_id: int = 20

    @classmethod
    def from_fixture(cls, fixture: Path, *, map_fixture: Path | None = None) -> Simulator:
        serial, snapshot = load_snapshot(fixture)
        sim = cls(serial=serial, snapshot=snapshot)
        if map_fixture is not None:
            sim.site_map = load_site_map(map_fixture)
        sim.firmware = str(snapshot.get("version") or sim.firmware)
        if not sim.site_map:
            sim.site_map = {
                "allchargingData": [snapshot_dock()],
                "chargingData": snapshot_dock(),
                "areas": [],
                "nogozones": [],
                "novisionzones": [],
                "elec_fence": [],
                "pathways": [],
                "sidewalks": [],
                "deadends": [],
                "mul_points": [],
            }
        return sim

    def sibling(self, serial: str, *, firmware: str | None = None) -> Simulator:
        """A second robot with its own serial and state, for multi-robot tests."""
        other = Simulator(
            serial=serial,
            snapshot=copy.deepcopy(self.snapshot),
            firmware=firmware or self.firmware,
            plans=copy.deepcopy(self.plans),
            site_map=copy.deepcopy(self.site_map),
            now=self.now,
        )
        other.snapshot["version"] = other.firmware
        return other

    # -- wiring

    def attach(self, transport: FakeTransport) -> None:
        """Answer publishes on a fake transport directly."""
        self._transport = transport
        transport.on_publish = self.handle
        # A real robot heartbeats within 5 s of a subscription; answer immediately.
        transport.on_subscribe = lambda _filter: self.tick()

    def attach_port(self, port: FakeTransport) -> None:
        """Publish through ``port`` without taking its hooks: a :class:`FakeBroker` owns those."""
        self._transport = port

    # -- outbound helpers

    def _compress(self) -> bool:
        return codec.firmware_wants_zlib(self.firmware) is not False

    def _deliver(self, leaf: str, value: Any, *, plain: bool = False) -> None:
        raw = codec.encode(value, compress=not plain and self._compress())
        if isinstance(self._transport, FakeTransport):
            self._transport.deliver(topics.device(self.serial, leaf), raw)

    def _feedback(self, topic: str, state: int, msg: str, data: Any) -> None:
        self._deliver("data_feedback", {"topic": topic, "state": state, "msg": msg, "data": data})

    def _blob(self, obj: Any) -> str:
        return base64.b64encode(zlib.compress(json.dumps(obj).encode())).decode()

    # -- clock

    def tick(self) -> None:
        """Advance the behavioural clock: auto-sleep, heartbeat, telemetry frame."""
        if self.awake and self.woke_at is not None and self.now() - self.woke_at > SLEEP_AFTER:
            self.awake = False
        self._deliver("heart_beat", {"working_state": 1 if self.awake else 0}, plain=True)
        if self.awake:
            frame = {k: v for k, v in self.snapshot.items() if k in PUSH_KEYS}
            frame.setdefault("StateMSG", {})
            self._deliver("DeviceMSG", frame)

    # -- inbound

    def handle(self, topic: str, payload: bytes) -> None:
        parsed = topics.parse(topic)
        if parsed is None or parsed.side != "app" or parsed.serial != self.serial:
            return
        value, enc = codec.decode(payload)
        # Firmware >= 3.9 silently drops plaintext commands; model that.
        if self._compress() and enc != "zlib":
            self.log.append((parsed.leaf, "DROPPED: not zlib"))
            return
        # Firmware before 3.9 cannot read zlib and drops it just as silently.
        if not self._compress() and enc == "zlib":
            self.log.append((parsed.leaf, "DROPPED: zlib"))
            return
        self.log.append((parsed.leaf, value))
        # The real broker echoes app publishes back to subscribers.
        if isinstance(self._transport, FakeTransport):
            self._transport.deliver(topic, payload)
        handler = getattr(self, f"cmd_{parsed.leaf}", None)
        if handler is not None:
            handler(value)

    # -- command handlers (verified behaviour on 3.14.11)

    def cmd_set_working_state(self, value: Any) -> None:
        if isinstance(value, dict) and value.get("state") == 1:
            self.awake = True
            self.woke_at = self.now()
            self.tick()

    def cmd_get_device_msg(self, value: Any) -> None:
        snap = dict(self.snapshot)
        snap["StateMSG"] = {**snap.get("StateMSG", {}), "working_state": 1 if self.awake else 0}
        self._feedback("get_device_msg", 0, "Device messages retrieved successfully.", snap)

    def cmd_get_state_msg(self, value: Any) -> None:
        self._feedback(
            "get_state_msg", 0, "State messages retrieved successfully.", {"value": [0] * 33}
        )

    def cmd_get_controller(self, value: Any) -> None:
        self.controller_holder = "session"
        self._feedback(
            "get_controller", 0, "Successfully connected to the physical controller.", ""
        )

    def _set_state(self, **fields: Any) -> None:
        self.snapshot = {
            **self.snapshot,
            "StateMSG": {**self.snapshot.get("StateMSG", {}), **fields},
        }
        self.tick()

    def cmd_resume(self, value: Any) -> None:
        """As seen on 3.14.11: a paused plan heads back to its area; there is no data_feedback."""
        state = self.snapshot.get("StateMSG", {})
        if state.get("planning_paused"):
            self._set_state(on_going_planning=3, planning_paused=0, error_code=0, plan_msg="")
        else:
            self._set_state(plan_msg="Goal canceled ")

    def cmd_pause(self, value: Any) -> None:
        """As seen on 3.14.11: planning 0, pause code 1 (manual); there is no data_feedback."""
        if self.snapshot.get("StateMSG", {}).get("on_going_planning") in (1, 3):
            self._set_state(on_going_planning=0, planning_paused=1, plan_msg="Goal canceled ")

    def cmd_stop(self, value: Any) -> None:
        """As seen on 3.14.11: the plan ends where it is, with no pause code and no reply."""
        state = self.snapshot.get("StateMSG", {})
        if state.get("on_going_planning") in (1, 2, 3) or state.get("planning_paused"):
            self._set_state(on_going_planning=0, planning_paused=0, plan_msg="Goal canceled ")

    def cmd_start_plan(self, value: Any) -> None:
        """As seen on 3.14.11: route calculation (2), or -12 when no plan or route exists."""
        known = {plan.get("id") for plan in self.plans if isinstance(plan, dict)}
        wanted = value.get("id") if isinstance(value, dict) else None
        self._set_state(on_going_planning=2 if wanted in known else -12, planning_paused=0)

    def cmd_cmd_recharge(self, value: Any) -> None:
        """As seen on 3.14.11: the fault clears and the robot sets off on its path home."""
        if isinstance(value, dict) and value.get("cmd") == 2:
            self._set_state(on_going_planning=0, on_going_recharging=1, error_code=0)

    def cmd_mower_head_sensor_switch(self, value: Any) -> None:
        """As seen on 3.14.11: state -99 queries, and the answer carries state 33."""
        if isinstance(value, dict) and value.get("state") == -99:
            self._feedback(
                "mower_head_sensor_switch", 33, "Mower sensor switch status query feedback.", ""
            )

    def cmd_read_all_plan(self, value: Any) -> None:
        self._feedback("read_all_plan", 0, "", {"data": list(self.plans)})

    def cmd_read_gps_ref(self, value: Any) -> None:
        self._feedback(
            "read_gps_ref",
            0,
            "",
            {"ref": {"latitude": -14.2973, "longitude": 35.3024}, "hgt": 87.6, "rtkFixType": 1},
        )

    def cmd_get_map(self, value: Any) -> None:
        self._feedback("get_map", 0, "", self._blob(self.site_map))

    def cmd_check_map_connectivity(self, value: Any) -> None:
        """As seen on 3.14.11 for areas linked to the dock: three empty lists."""
        self._feedback(
            "check_map_connectivity", 0, "", {"disconnected": [], "invalid": [], "normal": []}
        )

    def cmd_get_all_map_backup(self, value: Any) -> None:
        self._feedback("get_all_map_backup", 0, "Map backups retrieved.", self._blob({"data": []}))

    def cmd_get_plan_feedback(self, value: Any) -> None:
        self._feedback("get_plan_feedback", -1, "no plan working", "")

    def cmd_read_global_params(self, value: Any) -> None:
        if isinstance(value, dict) and value.get("id") == 1:
            self._feedback(
                "read_global_params",
                0,
                "",
                {"id": 0, "plan_speed": 0.6, "enable_elec_fence": False, **self.global_params},
            )
        else:
            self._feedback(
                "read_global_params",
                -2,
                "An error occurred while reading global parameters. Please try again.",
                "",
            )

    def cmd_read_schedules(self, value: Any) -> None:
        self._feedback("read_schedules", 0, "", list(self.schedules))

    # -- writes (replies as seen on 3.14.11)

    def cmd_save_plan(self, value: Any) -> None:
        """Without an id the robot numbers the plan itself; the reply does not carry it."""
        plan = dict(value) if isinstance(value, dict) else {}
        if "id" not in plan:
            plan["id"] = max((int(p.get("id", 0)) for p in self.plans), default=0) + 1
        self.plans = [p for p in self.plans if p.get("id") != plan["id"]] + [plan]
        self._feedback("save_plan", 0, "The plan was saved successfully.", "")

    def cmd_del_plan(self, value: Any) -> None:
        """Deleting a plan deletes the schedules that use it."""
        wanted = value.get("id") if isinstance(value, dict) else None
        self.plans = [p for p in self.plans if p.get("id") != wanted]
        self.schedules = [s for s in self.schedules if s.get("plan_id") != wanted]
        self._feedback("del_plan", 0, "Plan deleted successfully.", "")

    def cmd_sort_plan(self, value: Any) -> None:
        order = list(value.get("ids") or []) if isinstance(value, dict) else []
        by_id = {p.get("id"): p for p in self.plans}
        self.plans = [by_id[i] for i in order if i in by_id] + [
            p for p in self.plans if p.get("id") not in order
        ]
        self._feedback("sort_plan", 0, "The plans have been sorted successfully.", "")

    def cmd_save_schedule(self, value: Any) -> None:
        """The reply is the stored record: what was sent plus the robot's own fields."""
        sent = dict(value) if isinstance(value, dict) else {}
        stored = {
            "completed_this_window": False,
            "interval_time": 0,
            "is_weather_schedule": 0,
            "times": 0,
            "timezone": "",
            **sent,
        }
        self.schedules = [s for s in self.schedules if s.get("id") != stored.get("id")]
        self.schedules.append(stored)
        self._feedback("save_schedule", 0, "", stored)

    def cmd_del_schedule(self, value: Any) -> None:
        wanted = value.get("id") if isinstance(value, dict) else None
        self.schedules = [s for s in self.schedules if s.get("id") != wanted]
        self._feedback("del_schedule", 0, "Schedule deleted successfully.", "")

    def cmd_set_sound_param(self, value: Any) -> None:
        if isinstance(value, dict):
            self._set_state(enable_sound=bool(value.get("enable")), volume=value.get("vol"))
        self._feedback("set_sound_param", 0, "Sound settings updated successfully.", "")

    def cmd_set_person_detect(self, value: Any) -> None:
        """The message says 'enabled' for both values, as on the real robot."""
        if isinstance(value, dict):
            self._set_state(person_detect_status=int(value.get("state") or 0))
        self._feedback("set_person_detect", 0, "Person detection enabled successfully.", "")

    def cmd_set_child_lock(self, value: Any) -> None:
        on = bool(value.get("state")) if isinstance(value, dict) else False
        self._set_state(child_lock_status=int(on))
        word = "enabled" if on else "disabled"
        self._feedback("set_child_lock", 0, f"set_child_lock success: {word}", "")

    def cmd_set_follow_state(self, value: Any) -> None:
        if isinstance(value, dict):
            self._set_state(robot_follow_state=int(value.get("state") or 0))

    def cmd_save_global_params(self, value: Any) -> None:
        sent = dict(value) if isinstance(value, dict) else {}
        changed = any(self.global_params.get(k) != v for k, v in sent.items() if k != "id")
        self.global_params = {**self.global_params, **{k: v for k, v in sent.items() if k != "id"}}
        msg = (
            "Global parameters updated successfully."
            if changed
            else "No changes detected, parameters unchanged."
        )
        self._feedback("save_global_params", 0, msg, "")

    def cmd_read_mower_area_params(self, value: Any) -> None:
        wanted = value.get("id") if isinstance(value, dict) else None
        stored = self.mower_area_params.get(wanted) if isinstance(wanted, int) else None
        self._feedback(
            "read_mower_area_params",
            0,
            "Mower area settings loaded successfully.",
            {**(stored or MOWER_AREA_PARAMS_DEFAULT), "id": wanted if stored else 0},
        )

    def cmd_save_mower_area_params(self, value: Any) -> None:
        sent = dict(value) if isinstance(value, dict) else {}
        area_id = sent.get("id")
        if isinstance(area_id, int):
            kept = {k: v for k, v in sent.items() if k != "mapping_module_type"}
            self.mower_area_params[area_id] = {
                **self.mower_area_params.get(area_id, MOWER_AREA_PARAMS_DEFAULT),
                **kept,
            }
        self._feedback("save_mower_area_params", 0, "", "")

    def _store_object(self, command: str, family: str, value: Any, extra: dict[str, Any]) -> None:
        record = dict(value) if isinstance(value, dict) else {}
        if "id" not in record:
            record["id"] = self.next_object_id
            self.next_object_id += 1
        stored = {**extra, **record}
        records = [r for r in self.site_map.get(family) or [] if r.get("id") != stored["id"]]
        self.site_map = {**self.site_map, family: [*records, stored]}
        self._feedback(command, 0, "", stored)

    def _delete_object(self, command: str, family: str, value: Any, msg: str) -> None:
        wanted = value.get("id") if isinstance(value, dict) else None
        records = [r for r in self.site_map.get(family) or [] if r.get("id") != wanted]
        self.site_map = {**self.site_map, family: records}
        self._feedback(command, 0, msg, "")

    def cmd_save_nogozone(self, value: Any) -> None:
        self._store_object("save_nogozone", "nogozones", value, {"area_id": -1})

    def cmd_del_nogozone(self, value: Any) -> None:
        self._delete_object("del_nogozone", "nogozones", value, "NoGoZone deleted successfully.")

    def cmd_save_pathway(self, value: Any) -> None:
        self._store_object("save_pathway", "pathways", value, {"start_id": 0, "end_id": 0})

    def cmd_del_pathway(self, value: Any) -> None:
        self._delete_object("del_pathway", "pathways", value, "Pathway deleted successfully.")

    def cmd_save_sidewalk(self, value: Any) -> None:
        self._store_object("save_sidewalk", "sidewalks", value, {"start_id": 0, "end_id": 0})

    def cmd_del_sidewalk(self, value: Any) -> None:
        self._delete_object("del_sidewalk", "sidewalks", value, "Sidewalk deleted successfully.")

    def cmd_save_clean_area(self, value: Any) -> None:
        """With an id the area is edited in place and the stored record comes back."""
        record = dict(value) if isinstance(value, dict) else {}
        current: dict[str, Any] = next(
            (a for a in self.site_map.get("areas") or [] if a.get("id") == record.get("id")), {}
        )
        self._store_object("save_clean_area", "areas", {**current, **record}, {})

    def cmd_preview_plan_path(self, value: Any) -> None:
        """Shaped like plan_feedback: a fill path and an edge lap per area."""
        wanted = value.get("id") if isinstance(value, dict) else None
        plan = next((p for p in self.plans if p.get("id") == wanted), None)
        if plan is None:
            self._feedback("preview_plan_path", -1, "plan not found", "")
            return
        areas = list(plan.get("areaIds") or [])
        paths = [
            {
                "clean_index": 0,
                "clean_times": 0,
                "id": area,
                "type": kind,
                "path_slope": [],
                "path": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}],
            }
            for area in areas
            for kind in (0, 1)
        ]
        self._feedback(
            "preview_plan_path",
            0,
            "",
            {
                "planId": wanted,
                "areaIds": areas,
                "cleanAreaId": -1,
                "cleanPathProgress": paths,
                "finishIds": [],
                "state": 0,
                "startTime": 0,
                "duration": 0,
                "actualCleanArea": 0.0,
                "finishCleanArea": 0.0,
                "totalCleanArea": 0.0,
                "leftTime": 600.0,
                "totalTime": 600.0,
            },
        )

    def cmd_mower_target_cmd(self, value: Any) -> None:
        """No reply; the lift motor goes to the target."""
        if isinstance(value, dict) and "target" in value:
            head = dict(self.snapshot.get("mower_head_info01") or {})
            head["lift_motor_place"] = value["target"]
            self.snapshot = {**self.snapshot, "mower_head_info01": head}

    def cmd_wireless_charging_cmd(self, value: Any) -> None:
        """No reply; cmd 1 on the dock starts charging."""
        if isinstance(value, dict) and value.get("cmd") == 1:
            self._set_state(charging_status=2)

    def cmd_read_recharge_point(self, value: Any) -> None:
        self._feedback("read_recharge_point", 0, "", snapshot_dock())

    def _map_list(self, name: str, family: str) -> None:
        self._feedback(name, 0, "", {"data": list(self.site_map.get(family) or [])})

    def cmd_read_all_nogozone(self, value: Any) -> None:
        self._map_list("read_all_nogozone", "nogozones")

    def cmd_read_all_clean_area(self, value: Any) -> None:
        self._map_list("read_all_clean_area", "areas")

    def cmd_read_all_pathway(self, value: Any) -> None:
        self._map_list("read_all_pathway", "pathways")

    def cmd_read_all_sidewalk(self, value: Any) -> None:
        self._map_list("read_all_sidewalk", "sidewalks")

    def cmd_read_area_params(self, value: Any) -> None:
        wanted = value.get("id") if isinstance(value, dict) else None
        known = {a.get("id") for a in self.site_map.get("areas") or []}
        self._feedback(
            "read_area_params",
            0,
            "Area settings details retrieved successfully.",
            {**AREA_PARAMS_DEFAULT, "id": wanted if wanted in known else 0},
        )

    def edit_map(self, family: str, record: dict[str, Any], *, command: str) -> None:
        """Change the stored map and emit the ack an app edit produces."""
        records = [r for r in self.site_map.get(family) or [] if r.get("id") != record.get("id")]
        records.append(record)
        self.site_map = {**self.site_map, family: records}
        self._feedback(command, 0, "", "")


MOWER_AREA_PARAMS_DEFAULT: dict[str, Any] = {
    "clean_times": 1,
    "clean_times_dis": 0.0,
    "current_blade_height": 0,
    "edge_circles": 2,
    "edge_direction_mode": 0,
    "gap": 0.3,
    "route_order": 0,
    "first_clean_params": {"blade_height": 50, "blade_speed": 80, "plan_speed": 0.5},
    "double_clean_params": {"blade_height": 0, "blade_speed": 80, "plan_speed": 0.5},
}

AREA_PARAMS_DEFAULT: dict[str, Any] = {
    "clean_times": 1,
    "contract_dis": 0.2,
    "edge_circles": 1,
    "edge_direction_mode": 0,
    "gap": 0.3,
    "route_offset_enable": True,
    "first_clean_params": {"roller_speed": 1600, "plan_speed": 0.4, "heavy_snow_mode": True},
    "double_clean_params": {"roller_speed": 1300, "plan_speed": 0.4, "heavy_snow_mode": False},
}

PUSH_KEYS = frozenset(
    {
        "BatteryMSG",
        "BodyMsg",
        "BodyVersionMsg",
        "CombinedOdom",
        "EletricMSG",
        "HeadAndVersionCheck",
        "HeadMsg",
        "HeadSerialMsg",
        "RTKMSG",
        "RunningStatusMSG",
        "StateMSG",
        "WheelSpeedMSG",
        "abnormal_msg",
        "combined_odom_confidence",
        "mower_head_info01",
        "mower_head_info02",
        "mower_head_info03",
        "mower_head_info04",
        "route_priority",
        "rtk_base_data",
        "rtcm_age",
        "rtcm_info",
        "timestamp",
        "ultrasonic_msg",
        "version",
        "wireless_recharge",
    }
)


def snapshot_dock() -> dict[str, Any]:
    return {
        "chargingPoint": {"phi": 0.0, "x": -0.054, "y": -0.207},
        "enable": True,
        "hasChargingStation": 1,
        "id": 1,
        "name": "",
        "startPoint": {"phi": 2.906, "x": -1.026, "y": 0.027},
        "straightPhi": 2.9055442810058594,
    }


async def run_on_broker(sim: Simulator, host: str, port: int = 1883, *, rate: float = 1.0) -> None:
    """Serve the simulator on a real broker (for contributors without a robot)."""
    from .transport import MqttTransport  # noqa: PLC0415 - optional path

    transport = MqttTransport(host, port, identifier=f"yarbo-sim-{sim.serial[-4:]}")

    class _Bridge(FakeTransport):
        pass

    bridge = _Bridge()
    bridge._connected = True
    sim.attach(bridge)
    await transport.connect()
    await transport.subscribe(f"snowbot/{sim.serial}/app/#")

    async def pump() -> None:
        while True:
            item = await bridge.inbox.get()
            if isinstance(item, Message):
                await transport.publish(item.topic, item.payload)

    async def clock() -> None:
        while True:
            sim.tick()
            await asyncio.sleep(1.0 / rate if sim.awake else 5.0)

    async def inbound() -> None:
        async for msg in transport.messages():
            sim.handle(msg.topic, msg.payload)

    await asyncio.gather(pump(), clock(), inbound())
