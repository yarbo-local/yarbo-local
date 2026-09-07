# Protocol knowledge base

This directory is the source of truth for what we know about the Yarbo local MQTT dialect. The library's command registry and the generated documentation are derived from these files, so the code can never disagree with the data.

| File | Contents |
|---|---|
| `commands.yaml` | One entry per command on `snowbot/{sn}/app/<name>`: payload schema, acknowledgement type, whether it needs the controller role, whether the robot must be awake, head compatibility, risk class, status, and evidence. |
| `fields.yaml` | The `DeviceMSG` field map: dotted path, type, unit, scaling, sentinels, and how a Home Assistant entity should present it. |
| `codes.yaml` | Enum tables for status codes. |
| `fixtures/` | Redacted captures, grouped by firmware version and scenario. Every `verified` entry above points at one. |

## Status rules

- `verified`: a fixture in `fixtures/` shows the request and the observed reply or effect on real hardware, and the firmware version is recorded. CI fails a pull request that marks a command `verified` without evidence.
- `candidate`: known from the robot's bridge headers, the vendor SDK, or another project's code, but not yet captured here. Candidates cannot be sent by the library; the Phase 0 `probe` tool has its own short allowlist.
- `forbidden`: never registered, never sendable. Listed only so nobody adds them by accident. See `commands.yaml` for the reasoning per group.

## Risk classes

- `safe`: reads, and writes that affect settings or a saved plan.
- `confirm`: writes that change the map, stop the robot, or power it down. Require an admin plus an explicit confirmation in any UI.
- `forbidden`: remote execution, network changes, factory calibration, mass deletion, firmware, and motion outside a saved plan.

## Contributing evidence

Capture with `yarbo-local sniff --redact --out capture.jsonl`, or probe one command with `yarbo-local probe <host> <command> --out probe.jsonl`. Review the file, drop it under `fixtures/<firmware>/<scenario>.jsonl`, and reference it from the command or field entry. Redaction is best-effort; look before you push.
