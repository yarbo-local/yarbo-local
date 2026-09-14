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

To run it on every commit that touches `protocol/`, install it as a local hook:

```bash
printf '#!/bin/sh\ngit diff --cached --name-only | grep -q "^protocol/" && exec uv run --quiet python scripts/leak_check.py\nexit 0\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```
