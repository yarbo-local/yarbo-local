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
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any
import zlib

from . import codec, topics
from .transport import FakeTransport, Message, Transport

SLEEP_AFTER = 200.0


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

    @classmethod
    def from_fixture(cls, fixture: Path) -> Simulator:
        serial, snapshot = load_snapshot(fixture)
        sim = cls(serial=serial, snapshot=snapshot)
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

    # -- wiring

    def attach(self, transport: FakeTransport) -> None:
        """Answer publishes on a fake transport directly."""
        self._transport = transport
        transport.on_publish = self.handle
        # A real robot heartbeats within 5 s of a subscription; answer immediately.
        transport.on_subscribe = lambda _filter: self.tick()

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
                {"id": 0, "plan_speed": 0.6, "enable_elec_fence": False},
            )
        else:
            self._feedback(
                "read_global_params",
                -2,
                "An error occurred while reading global parameters. Please try again.",
                "",
            )

    def cmd_read_schedules(self, value: Any) -> None:
        self._feedback("read_schedules", 0, "", [])

    def cmd_read_recharge_point(self, value: Any) -> None:
        self._feedback("read_recharge_point", 0, "", snapshot_dock())

    def _empty_list(self, name: str) -> None:
        self._feedback(name, 0, "", {"data": []})

    def cmd_read_all_nogozone(self, value: Any) -> None:
        self._empty_list("read_all_nogozone")

    def cmd_read_all_clean_area(self, value: Any) -> None:
        self._empty_list("read_all_clean_area")

    def cmd_read_all_pathway(self, value: Any) -> None:
        self._empty_list("read_all_pathway")

    def cmd_read_all_sidewalk(self, value: Any) -> None:
        self._empty_list("read_all_sidewalk")


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
