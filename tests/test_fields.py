"""protocol/fields.yaml is generated; these tests pin its contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1] / "protocol"
ENTITIES = {
    "sensor",
    "binary_sensor",
    "switch",
    "number",
    "light",
    "update",
    "device_tracker",
    None,
}
STATUSES = {"verified", "candidate"}


def _load() -> dict[str, Any]:
    with (ROOT / "fields.yaml").open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def test_fields_shape() -> None:
    data = _load()
    fields = data["fields"]
    assert len(fields) > 300, "field map looks truncated"
    for path, entry in fields.items():
        assert entry["status"] in STATUSES, path
        assert isinstance(entry["types"], list), path
        assert entry["types"], path
        assert isinstance(entry["seen"], dict), path
        if "entity" in entry:
            assert entry["entity"] in ENTITIES, f"{path}: entity {entry['entity']!r}"
        if "scale" in entry:
            assert isinstance(entry["scale"], int | float), path
        if "heads" in entry:
            assert all(isinstance(h, int) for h in entry["heads"]), path


def test_core_paths_present_and_typed() -> None:
    fields = _load()["fields"]
    core = {
        "BatteryMSG.capacity": "sensor",
        "StateMSG.on_going_planning": "sensor",
        "StateMSG.planning_paused": "sensor",
        "StateMSG.on_going_recharging": "sensor",
        "HeadMsg.head_type": "sensor",
        "RTKMSG.status": "sensor",
        "rtk_base_data.rover.gngga": "device_tracker",
        "version": "update",
        "StateMSG.volume": "number",
    }
    for path, entity in core.items():
        assert path in fields, path
        assert fields[path].get("entity") == entity, path


def test_codes_references_resolve() -> None:
    fields = _load()["fields"]
    with (ROOT / "codes.yaml").open(encoding="utf-8") as fh:
        codes = yaml.safe_load(fh)
    for path, entry in fields.items():
        ref = entry.get("codes")
        if ref:
            assert ref in codes, f"{path}: codes ref {ref!r} missing from codes.yaml"
