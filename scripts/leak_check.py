"""Check fixtures for this site's real position before they are committed.

Usage: uv run python scripts/leak_check.py [--captures captures] [PATH ...]

Needles come from your own unredacted captures (the gitignored ``captures/``
folder): every GGA fix and every latitude/longitude found there, including
inside JSON strings and base64-zlib map blobs. The fixtures, the field map and
any extra paths are then searched the same way. Prints counts only, never the
coordinates. Exit status 1 when anything matches. Without captures there is
nothing to compare against, so it warns and passes.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from yarbo_local import codec  # noqa: E402

GGA = re.compile(r"\$G[NP]GGA,[^,]*,(\d{4}\.\d+),([NS]),(\d{5}\.\d+),([EW])")
KEYED = re.compile(r'"(?:latitude|longitude|lat|lon|lan)": (-?\d{1,3}\.\d{3,})')
SERIAL_TOPIC = re.compile(r"snowbot/([0-9A-Z]{12,20})/")


def texts(value: Any) -> Iterator[str]:
    """Every string form of a payload, with nested JSON strings and blobs decoded."""
    yield json.dumps(value)
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            blob = codec.decode_blob(item)
            if blob is not None:
                yield json.dumps(blob[0])
                stack.append(blob[0])


def _dm(text: str, degrees: int) -> float:
    return float(text[:degrees]) + float(text[degrees:]) / 60


def needles_from(captures: Path) -> tuple[set[str], set[str]]:
    coords: set[str] = set()
    serials: set[str] = set()
    for path in sorted(captures.glob("*.jsonl")):
        for line in path.open(encoding="utf-8", errors="ignore"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            serials.update(SERIAL_TOPIC.findall(str(rec.get("topic", ""))))
            for text in texts(rec.get("payload")):
                for m in GGA.finditer(text):
                    lat = _dm(m.group(1), 2) * (-1 if m.group(2) == "S" else 1)
                    lon = _dm(m.group(3), 3) * (-1 if m.group(4) == "W" else 1)
                    coords.update({f"{lat:.3f}", f"{lon:.3f}", m.group(1)[:6], m.group(3)[:7]})
                for m in KEYED.finditer(text):
                    value = float(m.group(1))
                    if 1 < abs(value) <= 180:
                        coords.add(f"{value:.3f}")
    return coords, {s for s in serials if not s.startswith("SN-")}


METRE_KEYS = frozenset({"x", "y", "phi"})


def leaf_hits(value: Any, needles: set[str], key: str = "") -> int:
    """Count needles in every leaf, decoding blobs.

    Numbers under x, y or phi are map-frame metres or radians; their digits can
    coincide with a latitude to three decimals without saying anything about where
    the site is, so they are not compared with decimal-degree needles.
    """
    if isinstance(value, dict):
        return sum(leaf_hits(v, needles, str(k)) for k, v in value.items())
    if isinstance(value, list):
        return sum(leaf_hits(v, needles, key) for v in value)
    if isinstance(value, str):
        blob = codec.decode_blob(value)
        inner = leaf_hits(blob[0], needles) if blob is not None else 0
        return inner + sum(value.count(n) for n in needles)
    if isinstance(value, int | float) and not isinstance(value, bool) and key in METRE_KEYS:
        return 0
    text = json.dumps(value)
    return sum(text.count(n) for n in needles)


def count_hits(path: Path, needles: set[str]) -> int:
    if path.suffix == ".jsonl":
        hits = 0
        for line in path.open(encoding="utf-8"):
            if line.strip():
                rec = json.loads(line)
                hits += leaf_hits(rec, needles)
        return hits
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".json":
        try:
            return leaf_hits(json.loads(text), needles)
        except json.JSONDecodeError:
            pass
    return sum(text.count(n) for n in needles)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--captures", type=Path, default=ROOT / "captures")
    ap.add_argument("paths", nargs="*", type=Path)
    args = ap.parse_args()
    if not args.captures.is_dir() or not any(args.captures.glob("*.jsonl")):
        print(
            f"leak check: no captures in {args.captures}; nothing to compare against",
            file=sys.stderr,
        )
        return 0
    coords, serials = needles_from(args.captures)
    files = [
        *sorted((ROOT / "protocol").rglob("*.jsonl")),
        *sorted((ROOT / "protocol").rglob("*.json")),
        ROOT / "protocol" / "fields.yaml",
    ]
    for extra in args.paths:
        files.extend(
            sorted([*extra.rglob("*.jsonl"), *extra.rglob("*.json")]) if extra.is_dir() else [extra]
        )
    bad = {}
    for path in files:
        n = count_hits(path, coords | serials)
        if n:
            bad[str(path)] = n
    print(
        f"leak check: {len(coords)} position needles and {len(serials)} serials from "
        f"{args.captures}; {len(files)} files; " + (f"MATCHES {bad}" if bad else "clean"),
        file=sys.stderr,
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
