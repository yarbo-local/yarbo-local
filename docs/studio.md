# The Studio

A local web UI for the people who make the protocol knowledge base better. It runs on any machine on the robot's LAN, talks only to the robot, and writes only into a checkout of this repository.

```bash
uv sync --extra dev            # or: pip install "yarbo-local[studio]"
uv run yarbo-local studio yarbo.localdomain --fallback-scan 192.168.50.0/24
# Studio at http://127.0.0.1:8765
```

Run it from a checkout and it finds `protocol/` on its own. Installed from a wheel, it is read-only unless you pass `--protocol-dir /path/to/yarbo-local/protocol`.

## Panes

**Stream.** Every topic on `snowbot/{serial}/#` with count, rate, age, encoding and size. The message log is filterable and pausable; click a row for the decoded payload. Below it, a frame-to-frame diff of `DeviceMSG` shows exactly which paths changed and from what to what. Own publishes echoed by the broker are marked.

**Knowledge.** Every path the robot has sent in this session is compared with `protocol/fields.yaml`. Unknown paths are listed with their type and example. *Promote* opens a form for entity type, device class, unit, scale, sentinel, category, head gating and notes. Writing it updates `protocol/fields.seed.yaml` and regenerates `fields.yaml`. Source `capture` marks the path verified; anything else stays a candidate.

**Fixtures.** Save the last N seconds of the ring buffer, redacted, as `protocol/fixtures/<firmware>/<name>.jsonl`. Serial, MAC, IP, Wi-Fi names and coordinates are replaced the same way `yarbo-local sniff --redact` does, and the result reports how many times the real serial still appears (it should be zero). The tests and the simulator pick new fixtures up automatically.

**Console.** Send a verified command with an editable payload and see the correlated reply with state, message, data and latency. Candidates are listed but cannot be sent; forbidden names cannot be sent from anywhere. Verifying a candidate is a deliberate act: capture its use from the phone app on the Stream pane, save a fixture, and change its status in `commands.yaml` with that evidence.

## What it does not do yet

The app observer (pairing a phone-app publish with its reply and offering "promote to registry") and the contribute flow (branch, commit, pull request) are on the roadmap. The plumbing for both is here: app-side publishes already appear in the stream and in saved fixtures.

## Security

The UI binds to `127.0.0.1` by default. There is no authentication; do not bind it to a network interface on a network you do not trust. It never contacts anything but the robot.
