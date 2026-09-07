# Phase 0: protocol validation on real hardware

Nothing in the Home Assistant integration gets written until these are answered on a real robot. Every answer becomes a fixture under `protocol/fixtures/` and a `verified` line in `protocol/commands.yaml`.

Setup:

```bash
uv tool install yarbo-local   # or: uv run yarbo-local ... from a checkout
yarbo-local discover 192.168.40.0/24 --wait 8
yarbo-local sniff <rover-ip> --out captures/idle.jsonl
yarbo-local dump captures/idle.jsonl --keys --app
```

Keep the phone app closed unless a step says otherwise. Keep the robot docked unless a step says otherwise.

## Checklist

1. **Broker inventory.** `discover` the LAN. Record every host answering on 1883, its MAC, and whether it carries `snowbot` traffic. Expected: the rover. Question: does anything else answer, and does the base station appear at all?
2. **Wake-up.** Robot asleep (heartbeat `working_state: 0`, no `DeviceMSG`). Run `probe <ip> set_working_state`. Does `DeviceMSG` start flowing? Does `heart_beat.working_state` flip to 1? Does it need `get_controller` first? Try both orders.
3. **Cadence.** Sniff 10 minutes asleep, 10 minutes awake and docked, 5 minutes with the phone app open. Record the rate of `heart_beat` and `DeviceMSG` in each state from `dump`.
4. **Compression.** From the same captures: encoding of each topic, and the `DeviceMSG.version` value. Confirm the 3.9.0 rule matches what the robot sends.
5. **Snapshot without the app.** `probe <ip> get_device_msg` while asleep and while awake. Does it answer when asleep? Does the reply carry sub-objects the push omits (`LedInfoMSG`, `HubInfoMSG`, `NetMSG`)?
6. **Map.** `probe <ip> get_map --timeout 30 --out captures/get_map.jsonl`. Record the envelope variant (object, string, base64 zlib), payload size, and which zone families are present.
7. **Plans.** `probe <ip> read_all_plan` asleep and awake. Record whether it answers asleep, and the reply shape (`data.data` list versus bare list).
8. **Write acknowledgement.** With the phone app: create a throwaway no-go zone while sniffing. Capture the app's `save_nogozone` request, the `data_feedback` reply, and whether the reply carries the new id. Then delete it the same way. Never send these from the probe tool.
9. **Controller theft.** Take the controller with `probe <ip> get_controller`, then open the phone app and press any control while sniffing. Record what the robot publishes at the moment the app takes over: anything on `data_feedback`, and changes in `StateMSG.machine_controller` or `car_controller`.
10. **Base station relay.** If step 1 found nothing besides the rover: find the base station in the DHCP table by MAC, port-scan it for 1883 and 8883, and try `discover` against it directly. Walk the rover out of Wi-Fi range while sniffing the rover's IP and note when traffic stops and what `route_priority` and `halow_status` report just before. Decide whether the relay exists on this hardware.
11. **Frame conventions.** Awake and docked, note `CombinedOdom.x/y/phi` and the GGA position. Push the robot one metre north (or drive it with the app) and record both again. Determine the sign of x and y and whether `phi` is degrees or radians, compass or mathematical.
12. **Broker behaviour.** From any capture: are app-side publishes echoed back to other subscribers? Does `discover` on port 8883 succeed? Does the broker accept a retained publish on a scratch topic (test on `yarbo-local/test`, never on `snowbot/`)?

## Open value questions to settle while there

- `cmd_recharge`: `cmd` 1 or 2. Observe what the app sends when you tap return-to-dock.
- `set_sound_param`: `vol` 0 to 1 or 0 to 100. Observe the app.
- `plan_feedback` key casing on the wire, and whether `get_plan_feedback` replies on `plan_feedback` or `data_feedback`.
- `BatteryMSG.status` values while docked, charging, and driving.
