# Fixtures

Redacted JSONL captures from real robots, one directory per firmware version, one file per scenario:

```
fixtures/
  3.9.2/
    idle-docked.jsonl
    wake-set_working_state.jsonl
    plan-start-to-finish.jsonl
    get_map.jsonl
```

Each line is one MQTT message as written by `yarbo-local sniff --redact` or `yarbo-local probe --out`. Serials are per-file tokens, coordinates are shifted, network details are removed. Review every file before committing it; redaction is best-effort.

A fixture is evidence. `protocol/commands.yaml` and `protocol/fields.yaml` reference fixtures by path, and CI refuses `verified` status without one.

## Before committing a fixture

Run the leak check. It reads your own unredacted captures from `captures/` (never committed), collects every position and serial in them, and searches all fixtures for them, including inside map blobs. It prints counts only.

```bash
uv run python scripts/leak_check.py
```

The repository ships the check as a pre-commit hook in `.githooks/`. Enable it once per clone, so a commit touching `protocol/` or `tests/` cannot go through with a leak in it:

```bash
git config core.hooksPath .githooks
```

`yarbo-local-ha` and `yarbo-local-card` carry the same hook for their own fixtures and demo data; it runs this script from a sibling clone of this repository.
