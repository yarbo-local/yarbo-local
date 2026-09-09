"""The DeviceMSG field map.

Two files under ``protocol/``:

- ``fields.seed.yaml`` is knowledge: curated semantics per path, edited by hand
  or through the Studio.
- ``fields.yaml`` is generated: every path the fixtures show, with observed
  types, an example, how often it appeared, and the seed's semantics merged on
  top. Never edit it; regenerate it.

``status`` is ``verified`` only when the seed says ``source: capture``, which
means the semantics were confirmed on our own robot.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any

import yaml

from . import codec

SEED_FILE = "fields.seed.yaml"
FIELDS_FILE = "fields.yaml"

ENTITY_TYPES = (
    "sensor",
    "binary_sensor",
    "switch",
    "number",
    "light",
    "device_tracker",
    "update",
    "select",
)
SEMANTIC_KEYS = (
    "entity",
    "device_class",
    "unit",
    "scale",
    "sentinel",
    "enabled",
    "category",
    "codes",
    "heads",
    "notes",
    "source",
)

_SEED_HEADER = (
    "# Curated DeviceMSG semantics: the part of the field map that is knowledge, not\n"
    "# inventory. Edit by hand or through the Studio's Knowledge pane; then run\n"
    "# scripts/gen_fields.py to regenerate fields.yaml, which merges this with what\n"
    "# the fixtures actually show.\n#\n"
    "# Keys per path: entity (sensor, binary_sensor, switch, number, light,\n"
    "# device_tracker, update, or null), device_class, unit, scale, sentinel,\n"
    "# enabled, category, codes (a key in codes.yaml), heads, notes, source.\n"
    "# source 'capture' means we confirmed the semantics on our own robot and the\n"
    "# path becomes verified in fields.yaml.\n\n"
)
_FIELDS_HEADER = (
    "# DeviceMSG field map. GENERATED from protocol/fixtures plus protocol/fields.seed.yaml\n"
    "# by yarbo_local.fieldmap (scripts/gen_fields.py). Edit the seed, not this file.\n"
    "#\n"
    "# status: verified means the semantics were confirmed on a capture; candidate\n"
    "# means the path was observed but its meaning comes from another project or is\n"
    "# unknown. entity is the Home Assistant platform it should become, or null.\n"
    "# heads lists the head types a field is meaningful for. scale multiplies the raw\n"
    "# value; sentinel is a raw value meaning 'no reading'.\n\n"
)


def load_seed(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fields = raw.get("fields") or {}
    return {str(k): dict(v or {}) for k, v in fields.items()}


def save_seed(path: Path, seed: dict[str, dict[str, Any]]) -> None:
    body = yaml.safe_dump(
        {"version": 1, "fields": seed}, sort_keys=False, allow_unicode=True, width=110
    )
    path.write_text(_SEED_HEADER + body, encoding="utf-8")


def load_fields(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): dict(v or {}) for k, v in (raw.get("fields") or {}).items()}


def inventory(fixtures: Path) -> dict[str, dict[str, Any]]:
    """Every flattened DeviceMSG path seen in the fixtures, push and snapshot."""
    paths: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"types": Counter(), "examples": [], "push": 0, "snapshot": 0}
    )

    def note(flat: dict[str, Any], kind: str) -> None:
        for key, value in flat.items():
            entry = paths[key]
            entry["types"][type(value).__name__] += 1
            entry[kind] += 1
            if len(entry["examples"]) < 2 and value not in entry["examples"]:
                entry["examples"].append(value)

    for path in sorted(fixtures.glob("*/*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            topic, payload = rec.get("topic", ""), rec.get("payload")
            if topic.endswith("/device/DeviceMSG") and isinstance(payload, dict):
                note(codec.flatten(payload), "push")
            elif (
                topic.endswith("/device/data_feedback")
                and isinstance(payload, dict)
                and payload.get("topic") == "get_device_msg"
                and isinstance(payload.get("data"), dict)
            ):
                note(codec.flatten(payload["data"]), "snapshot")
    return paths


def firmware_seen(fixtures: Path) -> list[str]:
    return sorted(p.name for p in fixtures.iterdir() if p.is_dir() and not p.name.startswith("."))


def build(fixtures: Path, seed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    inv = inventory(fixtures)
    fields: dict[str, Any] = {}
    for path in sorted(inv):
        obs = inv[path]
        entry: dict[str, Any] = {
            "types": sorted(obs["types"]),
            "example": obs["examples"][0] if obs["examples"] else None,
            "seen": {"push": obs["push"], "snapshot": obs["snapshot"]},
            "status": "candidate",
        }
        curated = seed.get(path)
        if curated:
            entry.update({k: v for k, v in curated.items() if v is not None or k == "entity"})
            entry["status"] = "verified" if curated.get("source") == "capture" else "candidate"
        fields[path] = entry
    return {
        "version": 1,
        "generated_by": "yarbo_local.fieldmap",
        "firmware_seen": firmware_seen(fixtures),
        "summary": {
            "paths": len(fields),
            "seeded": sum(1 for p in fields if p in seed),
            "seed_paths_not_in_fixtures": [p for p in seed if p not in inv],
        },
        "fields": fields,
    }


def render(data: dict[str, Any]) -> str:
    return _FIELDS_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def regenerate(protocol_dir: Path) -> dict[str, Any]:
    """Rebuild ``fields.yaml`` from the seed and the fixtures; return the data."""
    seed = load_seed(protocol_dir / SEED_FILE)
    data = build(protocol_dir / "fixtures", seed)
    (protocol_dir / FIELDS_FILE).write_text(render(data), encoding="utf-8")
    return data


def clean_semantics(semantics: dict[str, Any]) -> dict[str, Any]:
    """Keep only known keys with meaningful values; validate the entity type."""
    out: dict[str, Any] = {}
    for key in SEMANTIC_KEYS:
        if key not in semantics:
            continue
        value = semantics[key]
        if value in (None, "", [], {}):
            if key == "entity":
                out[key] = None
            continue
        if key == "entity" and value not in ENTITY_TYPES:
            raise ValueError(f"entity must be one of {ENTITY_TYPES}, not {value!r}")
        if key == "heads":
            value = [int(h) for h in value]
        if key in ("scale", "sentinel"):
            value = float(value) if isinstance(value, str) and "." in value else value
        if key == "enabled":
            value = bool(value)
        out[key] = value
    return out


def promote(protocol_dir: Path, path: str, semantics: dict[str, Any]) -> dict[str, Any]:
    """Write curated semantics for ``path`` into the seed and regenerate the map."""
    if not path or "\n" in path:
        raise ValueError("path is required")
    seed_path = protocol_dir / SEED_FILE
    seed = load_seed(seed_path)
    seed[path] = {**seed.get(path, {}), **clean_semantics(semantics)}
    save_seed(seed_path, seed)
    data = regenerate(protocol_dir)
    return dict(data["fields"].get(path) or seed[path])
