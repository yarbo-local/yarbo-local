"""The protocol knowledge base is data with rules; this test enforces the rules.

- every command has payload, ack, risk and status
- status is verified or candidate
- verified entries name a firmware version and an evidence fixture that exists
- forbidden names never appear under commands
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1] / "protocol"
ACKS = {"data_feedback", "plan_feedback", "recharge_feedback", "none"}
RISKS = {"safe", "confirm"}
STATUSES = {"verified", "candidate"}


def _load() -> dict[str, Any]:
    with (ROOT / "commands.yaml").open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def test_registry_shape() -> None:
    data = _load()
    assert data["commands"], "registry is empty"
    for name, entry in data["commands"].items():
        assert isinstance(entry, dict), name
        for key in ("payload", "ack", "risk", "status"):
            assert key in entry, f"{name} lacks {key}"
        assert entry["ack"] in ACKS, f"{name}: ack {entry['ack']!r}"
        assert entry["risk"] in RISKS, f"{name}: risk {entry['risk']!r}"
        assert entry["status"] in STATUSES, f"{name}: status {entry['status']!r}"


def test_verified_entries_have_evidence() -> None:
    data = _load()
    for name, entry in data["commands"].items():
        if entry["status"] != "verified":
            continue
        assert entry.get("firmware"), f"{name}: verified without firmware"
        evidence = entry.get("evidence")
        assert evidence, f"{name}: verified without evidence"
        assert (ROOT / evidence).is_file(), f"{name}: evidence file missing: {evidence}"


def test_forbidden_never_registered() -> None:
    data = _load()
    registered = set(data["commands"])
    for group, names in data["forbidden"].items():
        for raw in names:
            name = str(raw).split(" ")[0]
            if name.endswith("*"):
                prefix = name[:-1]
                hits = [r for r in registered if r.startswith(prefix)]
                assert not hits, f"{group}: {hits} match forbidden pattern {name}"
            else:
                assert name not in registered, f"{group}: {name} is registered"


def test_fixtures_are_redacted() -> None:
    """A fixture must never carry a raw 16-character Yarbo serial in a topic."""
    for path in ROOT.glob("fixtures/*/*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            assert (
                '"topic":"snowbot/SN-' in line
                or '"topic": "snowbot/SN-' in line
                or "snowbot/" not in line
            ), f"{path.name}: unredacted topic: {line[:80]}"
