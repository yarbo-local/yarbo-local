"""Discovery identifies a robot by its topics, and the library never runs another program."""

from __future__ import annotations

import ast
from pathlib import Path

from yarbo_local import discover, topics

SRC = Path(__file__).resolve().parents[1] / "src" / "yarbo_local"
NO_PROCESS_MODULES = {"subprocess", "pty", "pexpect"}


def test_classify_reports_only_what_was_observed() -> None:
    assert discover.classify(None, ["SN-1"]) == "carries snowbot traffic"
    assert "hostname contains yarbo" in discover.classify("YARBO.lan", ["SN-1"])
    assert discover.classify("yarbo.lan", []) == "hostname contains yarbo, but no snowbot traffic"
    assert discover.classify("printer.lan", []) == "open 1883 but no snowbot traffic"


def test_expand_hosts() -> None:
    assert discover.expand("10.0.0.5") == ["10.0.0.5"]
    assert discover.expand("10.0.0.5, 10.0.0.6") == ["10.0.0.5", "10.0.0.6"]
    hosts = discover.expand("192.168.50.0/30")
    assert hosts == ["192.168.50.1", "192.168.50.2"]


def test_heartbeat_wildcard_matches_a_heartbeat_topic() -> None:
    assert topics.heartbeats() == "snowbot/+/device/heart_beat"
    parsed = topics.parse("snowbot/SN-abc/device/heart_beat")
    assert parsed is not None
    assert parsed.serial == "SN-abc"


def test_a_hit_has_no_mac() -> None:
    assert "mac" not in discover.BrokerHit.__dataclass_fields__


def test_the_library_never_runs_another_program() -> None:
    """The proposal rules out ARP scans and shelling out; Home Assistant imports this code."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and (node.attr in {"system", "popen"} or node.attr.startswith("exec"))
            ):
                names = ["subprocess"]
            offenders += [f"{path.name}: {n}" for n in names if n in NO_PROCESS_MODULES]
    assert offenders == []
