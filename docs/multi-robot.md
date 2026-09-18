# More than one robot

Nobody on this project owns two Yarbos. Everything below is either measured on one robot, reasoned from the protocol, or proven against the simulator, and each line says which. If you have two or more robots, five minutes of your time settles the open questions: see [Check your site](#check-your-site).

## How it works

Every robot runs its own MQTT broker and puts its serial number in every topic (`snowbot/<serial>/...`). Two rovers on Wi-Fi are two brokers at two addresses. A robot sends a heartbeat every 2 s awake and every 5 s asleep (measured), so listening to a broker for six seconds hears every robot it carries.

The serial is the robot's identity everywhere: the Home Assistant entry, its entities, its device, its obstacle log and its aerial photo alignment are all keyed by it. A rover and its base station relay answer with the same data at two addresses (measured); because both carry one serial, they are one robot.

## What is handled

| Situation | What happens | Basis |
|---|---|---|
| Two rovers, two addresses | One Home Assistant entry each. A subnet scan finds both. | simulated |
| One robot at two addresses (rover and relay) | One entry. The second address is refused as already configured. Two sessions at once work through both. | measured |
| One broker carrying several robots | Setup lists the robots and asks which one. A session for one robot subscribes to its serial alone. | simulated |
| A DHCP lease moves, and robot A's old address now answers as robot B | The old address is refused because A is not heard there, and the subnet is scanned for A. | simulated |
| Robots on different firmware | The encoding (zlib from 3.9, plain JSON before) is decided per robot. A sleeping robot says nothing about its firmware, so the first request may be a guess; a guess that gets no reply is retried the other way. | simulated |
| Finding one robot again on a site with several | The scan stops the moment that robot is heard. | simulated |

## What is not known

1. **Does a base station ever relay more than one rover?** If it does, one address carries several serials. The code handles that, but nobody has seen it.
2. **Do brokers limit how many clients may connect?** Home Assistant holds one session per robot, the phone app another.
3. **Does the heartbeat interval hold on other firmware?** Discovery waits six seconds.

## Not supported, on purpose

Two robots on one map card. Each robot keeps its own map with its own reference point, so a card shows one robot. The Studio and the command line tools work with one robot per run.

## Check your site

```bash
git clone https://github.com/yarbo-local/yarbo-local
cd yarbo-local
uv sync
uv run yarbo-local sitecheck 192.168.50.0/24
```

Use the subnet your robots are on; a single IP or a comma list works too. It takes about a minute.

**What it does to your robots: nothing.** It listens, and it sends three read requests (`get_device_msg`, `read_all_plan`, `get_map`). It does not wake a robot, does not take control away from the phone app, and cannot send anything that moves a robot.

**What it writes:** `sitecheck-report.md` and `sitecheck.log` in the current directory. Serial numbers become `robot-1`, `robot-2`; addresses become `host-1`, `host-2`; positions are never written, only whether two of them match. Both files are safe to attach to a [GitHub issue](https://github.com/yarbo-local/yarbo-local/issues), and that is the whole request: run it, attach both files, and say whether the Yarbo app was open.

Each finding has the same shape, so a failure tells us what to change:

```
[FAIL] heartbeat-window: a robot heartbeats more slowly than discovery waits
  expected: every robot heard at least every 6s (we measured 2 s awake, 5 s asleep)
  seen: robot-2 on host-1: 2 heartbeats in 12s, longest gap 9.0s
  meaning: Discovery and address recovery listen for one window and would miss this robot. ...
  send us: the report file, sitecheck-report.md, and sitecheck.log; both are already scrubbed
```

| Check | Question it answers |
|---|---|
| `found` | Can this machine reach any robot at all? |
| `shared-broker` | Does any address carry more than one robot? The main open question. |
| `heartbeat-window` | Is every robot heard within the six seconds discovery waits? |
| `isolation` | Does a subscription for one serial ever deliver another robot's messages? |
| `reads` | Does every robot answer reads through every address that carries it? |
| `coexistence` | Do several sessions stay connected together? |
| `identity` | Do replies come from the robot they were addressed to? Compared by position, never written. |
| `encoding` | Does each robot's encoding match the firmware rule? |
| `unscoped` | Does a session given no serial notice the robots it did not pick? |
| `resolve` | From a stale address, is each robot found again at an address that really carries it? |
| `maps` | Do robots share one map frame, or keep one each? |

The same checks run under pytest, if you prefer: `uv run pytest tests/live --hosts 192.168.50.0/24 -v`.

The tool is tested before it reaches you: `tests/test_sitecheck.py` runs it against simulated sites and hands it records of sites that misbehave, one per failure above, so a failure on your site is about your site.
