"""Regenerate protocol/fields.yaml from protocol/fields.seed.yaml and protocol/fixtures.

Usage: uv run python scripts/gen_fields.py [--protocol-dir protocol]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from yarbo_local import fieldmap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--protocol-dir", type=Path, default=Path("protocol"))
    args = ap.parse_args()
    data = fieldmap.regenerate(args.protocol_dir)
    summary = data["summary"]
    verified = sum(1 for f in data["fields"].values() if f.get("status") == "verified")
    print(
        f"wrote {args.protocol_dir / fieldmap.FIELDS_FILE}: {summary['paths']} paths, "
        f"{summary['seeded']} seeded, {verified} verified; "
        f"seed paths not in fixtures: {summary['seed_paths_not_in_fixtures']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
