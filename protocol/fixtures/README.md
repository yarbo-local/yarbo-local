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
