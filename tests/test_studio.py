"""The Studio server against the simulator."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
import shutil

import pytest

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer

from yarbo_local import FakeTransport, Simulator, YarboRobot, fieldmap
from yarbo_local.studio.server import Studio

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "protocol/fixtures/3.14.11/get_device_msg-asleep.jsonl"


@pytest.fixture
def protocol_dir(tmp_path: Path) -> Path:
    fixtures = tmp_path / "fixtures" / "3.14.11"
    fixtures.mkdir(parents=True)
    shutil.copy(FIXTURE, fixtures)
    shutil.copy(REPO / "protocol/fields.seed.yaml", tmp_path)
    shutil.copy(REPO / "protocol/commands.yaml", tmp_path)
    fieldmap.regenerate(tmp_path)
    return tmp_path


@pytest.fixture
def sim() -> Simulator:
    return Simulator.from_fixture(FIXTURE)


async def _studio(sim: Simulator, protocol_dir: Path | None) -> tuple[Studio, YarboRobot]:
    transport = FakeTransport()
    sim.attach(transport)
    robot = YarboRobot(transport, serial=sim.serial)
    studio = Studio(robot, protocol_dir=protocol_dir)  # before start, to see the first heartbeat
    await robot.start(1.0)
    return studio, robot


@pytest.fixture
async def client(sim: Simulator, protocol_dir: Path) -> AsyncIterator[TestClient]:
    studio, robot = await _studio(sim, protocol_dir)
    async with TestClient(TestServer(studio.build_app())) as client:
        yield client
    await studio.close()
    await robot.close()


async def test_summary_and_stream(sim: Simulator, client: TestClient) -> None:
    summary = await (await client.get("/api/summary")).json()
    assert summary["serial"] == sim.serial
    assert summary["editable"] is True
    assert any(t["topic"].endswith("heart_beat") for t in summary["topics"])

    async with client.ws_connect("/ws") as ws:
        hello = await ws.receive_json()
        assert hello["type"] == "hello"
        sim.awake = True
        sim.woke_at = sim.now()
        sim.tick()
        got = [await ws.receive_json() for _ in range(3)]
        assert {g["type"] for g in got} == {"message", "diff"}
        frame = next(g for g in got if g["type"] == "diff")
        assert "BatteryMSG.capacity" in frame["added"]

    r = await client.get("/")
    assert r.status == 200
    assert "studio-app" in await r.text()


async def test_knowledge_diff_and_promote(client: TestClient, protocol_dir: Path) -> None:
    # A snapshot reply teaches the Studio every path the robot reports.
    assert (await client.post("/api/command", json={"name": "get_device_msg"})).status == 200
    diff = await (await client.get("/api/diff")).json()
    assert diff["unknown"] == []  # everything the simulator sends is in the field map
    assert diff["seen"] > 100

    r = await client.post(
        "/api/fields",
        json={
            "path": "abnormal_msg.ctrl_error_code",
            "entity": "sensor",
            "category": "diagnostic",
            "source": "capture",
        },
    )
    assert r.status == 200
    assert (await r.json())["status"] == "verified"
    seed = fieldmap.load_seed(protocol_dir / fieldmap.SEED_FILE)
    assert seed["abnormal_msg.ctrl_error_code"]["entity"] == "sensor"

    r = await client.post("/api/fields", json={"path": "x", "entity": "thermostat"})
    assert r.status == 400


async def test_fixture_export_is_redacted(sim: Simulator, client: TestClient) -> None:
    assert (await client.post("/api/command", json={"name": "get_device_msg"})).status == 200
    r = await client.post("/api/fixtures", json={"name": "Studio Test!", "seconds": 60})
    assert r.status == 200
    saved = await r.json()
    path = Path(saved["path"])
    assert path.name == "Studio-Test.jsonl"
    assert path.parent.name == "3.14.11"
    assert saved["records"] >= 2
    assert saved["leaks"] == {"serial": 0}
    assert sim.serial not in path.read_text(encoding="utf-8")
    listed = await (await client.get("/api/fixtures")).json()
    assert any(item["name"] == "Studio-Test" for item in listed)


async def test_console_obeys_registry(client: TestClient) -> None:
    r = await client.post("/api/command", json={"name": "read_all_plan"})
    assert r.status == 200
    reply = await r.json()
    assert reply["ok"] is True
    assert reply["latency_ms"] >= 0

    r = await client.post("/api/command", json={"name": "start_plan", "payload": {"id": 1}})
    assert r.status == 400
    assert "candidate" in (await r.json())["error"]

    r = await client.post("/api/command", json={"name": "erase_map"})
    assert r.status == 400
    assert "forbidden" in (await r.json())["error"]

    r = await client.post("/api/wake")
    assert (await r.json())["awake"] is True


async def test_read_only_without_checkout(sim: Simulator) -> None:
    studio, robot = await _studio(sim, None)
    async with TestClient(TestServer(studio.build_app())) as client:
        assert (await (await client.get("/api/summary")).json())["editable"] is False
        assert (await client.post("/api/fixtures", json={"name": "x"})).status == 400
        knowledge = await (await client.get("/api/knowledge")).json()
        assert "BatteryMSG.capacity" in knowledge["fields"]  # packaged copy
        assert "data_destruction" in knowledge["forbidden"]
    await studio.close()
    await robot.close()
