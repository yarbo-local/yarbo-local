# yarbo-local

Local-first tooling and, later, the asyncio library behind a Home Assistant integration for Yarbo robots. It talks to the anonymous MQTT broker the robot runs on your LAN and to nothing else. No Yarbo account, no vendor servers, no telemetry.

Status: **Phase 0, pre-alpha.** This repository currently contains the capture and probe tooling used to validate the protocol on real hardware, plus the protocol knowledge base that everything else will be generated from. The library API, the Home Assistant integration (`yarbo-local-ha`) and the dashboard card (`yarbo-local-card`) come after Phase 0 is answered. The plan is in the proposal linked below.

## What is here

| Path | Purpose |
|---|---|
| `src/yarbo_local/codec.py` | zlib-or-plain JSON codec with the firmware rule and an observed-encoding fallback. |
| `src/yarbo_local/capture.py` | `sniff`: subscribe to `snowbot/+/#` and write every message to JSONL, optionally redacted. |
| `src/yarbo_local/probe.py` | `probe`: send one allowlisted command, show the correlated reply and the telemetry deltas. |
| `src/yarbo_local/discover.py` | `discover`: find brokers carrying `snowbot` traffic and classify them. |
| `src/yarbo_local/dump.py` | `dump`: summarise a capture, including the full `DeviceMSG` key inventory. |
| `src/yarbo_local/redact.py` | Serial, MAC, IP, coordinate and Wi-Fi redaction for shareable fixtures. |
| `protocol/` | The knowledge base: `commands.yaml`, `fields.yaml`, `codes.yaml`, `fixtures/`. |
| `docs/phase0.md` | The twelve questions Phase 0 answers, with the exact commands. |

## Quick start

```bash
git clone https://github.com/yarbo-local/yarbo-local
cd yarbo-local
uv sync --extra dev
uv run yarbo-local discover 192.168.40.0/24
uv run yarbo-local sniff 192.168.40.23 --out captures/idle.jsonl
uv run yarbo-local dump captures/idle.jsonl --keys --app
uv run yarbo-local probe 192.168.40.23 get_device_msg
```

`probe` only sends commands on a short allowlist of reads plus the wake-up command. It will not send anything that moves the robot, changes the map, or touches settings.

## Can't see the base station in your router or controller?

You won't. The base station bridges the rover's HaLow link onto your LAN behind a locally administered MAC that never associates with an access point or asks your DHCP server for an address. It picks a static address for itself, inside your DHCP pool, and it has no management page. Network controllers such as UniFi list clients by association or wired port, so the base station simply does not exist to them, even while it answers on ports 1883, 8883 and 22.

Do not spend an afternoon on this. Scan the subnet instead:

```bash
uv run yarbo-local discover 192.168.1.0/24 --wait 8
```

Every host carrying Yarbo traffic is listed with its MAC and a classification. The rover shows up under the vendor's Wi-Fi radio; the base station shows up behind the bridge MAC. Then protect yourself from the address collision Yarbo set up for you: either shrink your DHCP pool so it starts above the base station's address, or put the Yarbo Wi-Fi association on an isolated VLAN so the whole bridge moves out of your main network.

## Safety and privacy

The robot's broker has no authentication. Anyone on its network segment can drive it. Put the robot on its own VLAN, allow only the machine running this tooling (and later Home Assistant) to reach port 1883, and think deliberately about whether the robot should have internet access at all. Captures contain your serial number, coordinates and network details; use `--redact` and review the file before sharing it.

## Contributing protocol knowledge

Read `protocol/README.md`. A command becomes `verified` only with a fixture showing the request and the reply on real hardware, with the firmware version recorded. Pull requests that add knowledge without evidence are asked for a capture.

## License and trademark

MIT. Protocol facts were assembled from the vendor's MIT-licensed SDK, from `python-yarbo`, `home-assistant-yarbo`, the `jtubb` and `briangann` forks of the vendor integration, and the `steves2j` map editor; see `protocol/commands.yaml` for per-entry sources. Yarbo is a trademark of its owner. This project is not affiliated with, endorsed by, or supported by Yarbo.
