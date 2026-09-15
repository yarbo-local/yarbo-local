# Phase 0 findings

Robot: one Yarbo body, no head attached, firmware 3.14.11, base station firmware 1.1.11, body MCU 3.2.26, chassis 2.1.40. Free-standing in the garage, off any charger, about 14.5 m from the mapped dock point. Earlier revisions of this file said "docked on the wired charger"; that was a misreading of `BodyMsg.recharge_state`, see section 13. Phone app closed throughout. All captures are redacted fixtures under `protocol/fixtures/3.14.11/`.

Numbering follows `phase0.md`.

## 1. Broker inventory

Two hosts answer on 1883 and 8883, both carry the same serial's traffic, both currently resolve to one locally administered MAC. The rover reports HaLow connected to the base station's SSID at -43 dBm with the HaLow route preferred (`route_priority` hg0 10, wlan0 600, wwan0 -1, no LTE). Earlier the rover's IP resolved to a different, globally administered MAC. Reading: the base station proxies the rover's IP while the rover is on HaLow, and the base station has its own IP as well. Both endpoints return the same snapshot with 52 ms and 62 ms latency. Which one runs the broker and which bridges is not yet distinguishable. The base station is reachable from the LAN on this site.

The `discover` scanner had a real bug: `asyncio.open_connection` resolves even IP literals through the default thread pool, so a /24 with dozens of concurrent probes timed out before connecting. Fixed with a raw non-blocking `sock_connect`.

## 2. Wake-up

`set_working_state {"state": 1, "source": "smart_home"}` works on the LAN. No acknowledgement on `data_feedback`. Within seconds `DeviceMSG` starts streaming and `heart_beat.working_state` flips to 1. No `get_controller` was needed. The robot puts itself back to sleep about 200 s after the last stimulus, so an unrenewed wake lasts roughly four and a half minutes. That is the reason the vendor integration renews every four minutes. Reads do not keep it awake. After a single wake with the app closed and the robot parked off the charger, `get_device_msg` was sent every 60 s. The heartbeat flipped back to 0 at t+301 s, one second after the fifth read, and `DeviceMSG` held 1 Hz until then (299 frames). A read that reset the sleep timer would have kept it awake past 440 s, so reads do not reset it. The window closed at 301 s rather than the roughly 200 s seen without reads, so either the window varies or there is a limit near 300 s from the wake itself. Reads kept answering after it slept. The consequence for the integration: continuous telemetry needs `set_working_state` re-sent before the window closes, and the vendor's four-minute renewal sits inside the measured window with little margin. Renewing does work: with `set_working_state` re-sent every 150 s after the first wake, the robot stayed awake for the whole 11-minute run (657 `DeviceMSG` frames at 1 Hz, still awake at the end). So the window is measured from the last wake command, not from the first, and reads do not count as a wake. A keep-awake that renews at 150 s has about a factor of two of margin.

## 3. Cadence

| State | `heart_beat` | `DeviceMSG` |
|---|---|---|
| asleep | every 5 s, `{"working_state": 0}` | none |
| awake, parked, no app | every 2 s, `{"working_state": 1}` | 1.00 Hz, zlib, about 1 KB |

The claim that `DeviceMSG` only streams while the phone app is connected is false on this firmware. It streams whenever the robot is awake.

## 4. Compression

`DeviceMSG.version` is `3.14.11`. Every device topic except `heart_beat` is zlib. Commands sent as zlib were accepted. The 3.9.0 rule holds.

## 5. Snapshot without the app

`get_device_msg` answers while asleep, 52 ms, state 0, `msg` "Device messages retrieved successfully.", with 52 top-level objects and 422 flattened paths. The push `DeviceMSG` has 29 top-level objects. Objects present only in the snapshot include `LedInfoMSG`, `HubInfoMSG`, `NetMSG`, `DebugMsg`, `system_info`, `camera_state`, `SoftwareUpdate`, `net_module_status`, `halow_status`, `route_priority`, `rtcm_*`, `hardware_version`, `chassis_version_msg`, `dc_version`, `base_name`, `min_app_version`.

Read-back exists in `StateMSG` for `person_detect_status`, `child_lock_status`, `enable_sound`, `volume` (float 0 to 1), `robot_follow_state`, `machine_controller`, `en_state_led`, `en_warn_led`, `lidar_switch`, `obstacle_toggle`, `sentry_mode_status`, `shipping_mode`. `LedInfoMSG` carries every LED channel including RGB and dimmer values per lamp. Assumed-state switches are unnecessary for all of these.

## 6. Map

`get_map {"width": 256, "height": 256}` answers asleep in 43 ms. On 3.14.11 the `data` field is a base64 string of zlib-compressed JSON, so that envelope is current, not legacy. Decoded keys: `allchargingData`, `chargingData`, `areas`, `nogozones`, `novisionzones`, `elec_fence`, `pathways`, `sidewalks`, `deadends`, `mul_points`. This site has no areas mapped yet; only the dock is present. Dock record: `chargingPoint {x, y, phi}`, `startPoint {x, y, phi}`, `straightPhi` in radians, `hasChargingStation`, `enable`, `id`, `name`. `get_all_map_backup` uses the same base64 zlib envelope.

## 7. Plans

`read_all_plan` answers asleep in 44 ms with `{"data": []}`. The claim that it goes unanswered when idle is false on this firmware. `read_schedules` returns a bare list, `read_all_clean_area`, `read_all_pathway`, `read_all_sidewalk`, `read_all_nogozone` return `{"data": []}`. Both reply shapes are real. `get_plan_feedback` with no plan running replies on `data_feedback` with state -1 and `msg` "no plan working", not on `plan_feedback`.

`read_global_params` needs `{"id": 1}`. With `{}` it replies state -2 "An error occurred while reading global parameters." With the id it returns about 60 settings including blade and blower battery cut-offs, `plan_speed`, `max_vel`, `enable_elec_fence`, `enable_weather_schedule`, chute angle limits, `default_plan_id`, and rain thresholds.

`read_recharge_point` answers asleep with the dock record. `read_gps_ref` answers asleep with `ref.latitude`, `ref.longitude`, `hgt`, `rtkFixType` 1 and a `lat_lon_hight` string. `get_state_msg` returns `{"value": [33 ints]}`, a bit array with unknown semantics.

## 8. Write acknowledgement

Answered in section 14: the app's saves are acknowledged on `data_feedback` with state 0.

Not tested. Needs the phone app to create a throwaway zone while sniffing.

## 9. Controller theft

Not tested. `StateMSG.machine_controller` reads 1 while idle with no app connected.

## 10. Base station relay

See 1. The relay is reachable and answers every command tried. The earlier belief that it was unreachable from the LAN is not what this site shows today.

Topology as observed with the network controller (UniFi) alongside ARP:

- The controller knows one Yarbo client: the physical Wi-Fi radio, hostname `YARBO`, associated to a 5 GHz AP, holding the DHCP lease for the rover's address.
- The base station's address is never leased. It is self-assigned inside the LAN's DHCP pool and is reachable only behind a locally administered bridge MAC that never associates with anything. The controller therefore has no record of it at all.
- ARP for the rover's address alternates between the physical radio MAC and the bridge MAC depending on which path answers first. The base station's address only ever answers from the bridge MAC. The base station's heartbeat also arrives faster.
- Both hosts present identical Ubuntu SSH banners and identical broker contents.

**Defect, not a design choice to accept.** A device that takes a static address inside the network's DHCP pool without asking, and appears only behind a bridge MAC that no controller can attribute, is a bad network citizen. It will collide with a lease sooner or later, it cannot be reserved, renamed or firewalled by MAC in the controller, and it has no management page to configure otherwise. The integration must discover the relay by probing the subnet, never by asking the controller, and the README must tell users to exclude the base station's address from their DHCP pool or move the whole Yarbo association to an isolated VLAN.

## 10b. Living on a VLAN

After moving the rover to an isolated VLAN with the host running the tooling on a different one:

- Reads and the wake-up work across the firewall exactly as on the flat LAN, 34 ms for a snapshot.
- Home Assistant's built-in discovery cannot fire: `dhcp` and `zeroconf` listen on HA's own segment, and the robot's DHCP happens on the other VLAN. The robot also advertises nothing over mDNS. Active subnet scanning is the only discovery that works on a segmented network, so it is the primary path in the design, not a fallback.
- The gateway registers the robot's DHCP hostname in local DNS, so `yarbo.localdomain` resolved to the new address the moment the lease moved. A DNS name is a better anchor than a reservation for a single robot. The hostname is a fixed firmware string, so two robots would collide on it.
- The base station kept its Default-LAN address, because its Ethernet enters through an access point port that cannot be assigned a VLAN. It never DHCPs, so it also has no DNS name. Until it is cabled to a switch port, it continues to relay the rover's full telemetry on the old network, and the isolation covers the rover's own radio only.

Resolution order the library should use when a connection drops: last known address, then configured DNS name, then subnet scan for the entry's serial.

## 11. Frame conventions

Not yet measured. Data points so far: `CombinedOdom` gives x 14.25, y 2.94, phi -0.20 while parked in the garage; the dock's `chargingPoint` is near the origin, so the robot sits about 14.5 m from it, and the dock's `straightPhi` of 2.91 says `phi` is radians. RTK heading in `RTKMSG.heading` is degrees. `RTKMSG.status` was `"1"` asleep and `"5"` awake, both strings.

## 12. Broker behaviour

The broker echoes app-side publishes to other subscribers: every probe saw its own command come back on `snowbot/{sn}/app/<cmd>`. That means the phone app's commands are observable, which the Studio's app-observer pane depends on.

- **TLS on 8883 works, anonymously.** The certificate is EMQX's stock sample: subject `C=CN, ST=hangzhou, O=EMQ, CN=Server`, issuer `EMQ RootCA`, valid 2020 to 2030, self-signed, TLS 1.3 with AES-256-GCM. It encrypts the hop but authenticates nothing, and every robot almost certainly presents the same certificate. The library can offer "encrypt only" on 8883 with verification off; it must not pretend that is authentication.
- **Retained messages:** the robot publishes nothing retained, so a fresh subscriber learns nothing until the next heartbeat. The broker itself honours retained publishes on other topics.
- **Last will:** the broker delivers a will after an abrupt disconnect. Useful for our own client's presence, useless for the robot's, which stays heartbeat-based.
- **Firmware version:** the broker is EMQX; the rover's snapshot shows `beam.smp` among top processes, consistent with an Erlang EMQX running on the robot itself.

## 13. Charging fields

The robot spent the whole of Phase 0 free-standing in the garage, off any charger, and the battery went from 100% to 71% over an evening with about 25 minutes awake in total. Throughout, `BodyMsg.recharge_state` read 3, which the community code maps to "wired charging (locked)" and uses to block plan start. That mapping is wrong on 3.14.11. The fields that agreed with reality were `BatteryMSG.status` 1 (vendor rule: above 1 means charging), `StateMSG.charging_status` 0, `wireless_recharge.state` 0, and `BatteryMSG.current` -300 mA, which is the pack discharging at rest. The library's `charging` now comes from `BatteryMSG.status` and `StateMSG.charging_status` only, and nothing is derived from `recharge_state`. Neither rule has been seen in the positive direction yet.

On 2026-09-14 the robot was back on the dock: odometry read x -0.00, y -0.18, on top of the dock's `chargingPoint` at -0.05, -0.21. Battery 100%, `BatteryMSG.status` 1, `StateMSG.charging_status` 0, `wireless_recharge.state` 0, `BatteryMSG.current` -300 to -400 mA, and `BodyMsg.recharge_state` **0**, against 3 off the dock. `wireless_recharge.output_voltage` read 10 on the dock and 15 off it. A full battery drawing 0.3 A from itself is consistent with a charger that has stopped at 100%, so this still does not show a charging value; it does show that `recharge_state` changes with docking, in the opposite direction to the community mapping. Fixture: `docked-awake-75s.jsonl`. A capture on the dock below 100% is what settles the charging rule.

## 14. Mapping through the app, and the map frame

On 2026-09-14 the driveway was mapped with the phone app while a passive capture ran on the rover's broker. Every app command and every reply passed through that broker, so the app observer idea works on this firmware without touching Yarbo's servers. Fixtures: `mapping-area-app.jsonl` (trimmed, redacted), `get_map-area-pathway.jsonl`, `area-params.jsonl`.

What the app sent, in order: `set_working_state` every 10 s for the whole session as its keep-awake; `cmd_vel` at about 5 Hz while driving by joystick (forbidden for this project); `cmd_roller` twice; `start_draw_cmd`; paired `set_auto_mapping_state {state, dir}`; `set_combined_odom_path_state {state, type}` to start and stop trail recording, during which the robot publishes `device/combined_odom_path` at 1 Hz; then `save_clean_area` without an id, `save_scan`, `read_area_params`, `save_area_params`, `save_clean_area` again with id 1 and `labels: [1]`, `read_all_clean_area`; and finally `save_pathway`, `read_pathway_params`, `save_pathway_params`. No `get_controller` was sent during the session. `start_draw_cmd`, `set_combined_odom_path_state` and `set_auto_mapping_state` stay on the forbidden motion list; mapping is something a person does with the app. `save_scan` is not registered because its purpose is unknown. Every save answered on `data_feedback` with state 0, which answers question 8: writes are acknowledged there like reads.

The robot stored an area "Area 1" (id 1, 25 vertices, 126.4 m2, stored `area` field) and a line "Pathway 1" (id 2, 5 vertices, 16.9 m, `start_id` 1, `end_id` 0). The app's default names were kept. `push_snow_dir` and `slope_degrees` of 9999.0 mean unset. `humps.ref` carries uninitialised denormal floats such as 5e-324. No plans exist yet.

**Map frame.** Zone `range` points are metres in a local frame whose origin is the zone's `ref` latitude and longitude, with **x pointing west and y pointing north**. Fitting 1,152 RTK-fixed samples of `CombinedOdom` against the rover's GGA fix from the same drive gives an RMS difference of 0.20 m, maximum 0.27 m, which is the antenna offset. The steves2j map card uses the same convention independently (`lon = ref_lon - x / m_per_deg_lon`). A first fit that allowed only rotation and scale failed badly, because a mirrored axis cannot be expressed that way. `CombinedOdom.phi` and vertex `phi` are radians measured from +x towards +y; in 339 of 345 moving steps the robot travelled along `phi`, and the other 6 were reversing.

**Redaction consequence.** The redactor shifts latitude and longitude by whole-degree offsets. Differences in latitude survive in metres, but metres per degree of longitude depend on the true latitude, so east-west distances computed from a redacted fixture are wrong by that factor. Tests on redacted fixtures check signs and the north axis only.

## 15. First plans with the Lawn Mower Pro

On 2026-09-14 and 15 the robot ran "east lawn plan" (area 4, 512 m2) and "west lawn plan" (area 9, 950 m2) with a Lawn Mower Pro head (`HeadMsg.head_type` 5, head firmware 2.2.4). Captures stay local; a centimetre-rounded plan and obstacle sample is in the integration's test fixtures.

**Blades.** `mower_head_info03` (left) and `mower_head_info04` (right) report `*_blade_motor_rpm` of about -2800 and +2800 (the left disc turns the other way), `*_blade_motor_speed` 80 (percent), `*_blade_motor_current` 70 to 131 varying with load, and `*_blade_motor_temp` 73 to 76 slowly rising. The user confirmed the head was cutting at the time. `mower_head_info02` (middle) reads zero in every field, commanded speed included; on this head it appears unused. `mower_head_info01.lift_motor_place` read 0 throughout; its scale is unknown.

**plan_feedback** arrives at 2 Hz while a plan runs: `{planId, areaIds, cleanAreaId, finishIds, actualCleanArea, finishCleanArea, totalCleanArea, duration, leftTime, totalTime, startTime, battery_consumption, runningState, state, cleanPathProgress}`. `cleanPathProgress` holds one entry per path of the area, `{id, type, clean_index, clean_times, path: [{x, y}], path_slope}`; the west lawn had a 129-point path of type 0 and an 82-point path of type 1, and `clean_index` advanced from 37 to 39 over 150 s on the first. `get_plan_feedback` answers state 0 with the same keys while a plan runs.

**cloud_points_feedback** also arrives at 2 Hz: `{rotate_rad, tmp_barrier_points}`, where `tmp_barrier_points` is a list of clusters, each a list of `{x, y}` in the map frame. The list empties within seconds, and it stayed empty for a whole 150 s window mid-plan, so a consumer has to accumulate clusters for the run. The integration merges clusters closer than 0.35 m and clears them when a new run starts.

**Error 901.** During the East Lawn run `StateMSG.error_code` went to 901 at 17:45 with the battery at 41 %, stayed there until 19:03, briefly returned to 0 while the robot started returning, went back to 901, and cleared at 19:59; the robot then returned to the dock. The meaning of 901 is unknown. Home Assistant's history holds the timeline; no capture was running.

**Charging, positive direction.** After that return, the library's charging rule (`BatteryMSG.status` above 1 or `StateMSG.charging_status` set) turned on at 20:03 while the battery rose from 36 % to 59 % in 33 minutes. That is the first observation of the rule firing correctly; which of the two fields fired was not captured.

## Values settled

- `set_sound_param.vol` scale: `StateMSG.volume` is a float 0 to 1, matching the vendor SDK.
- `data_feedback.state` is 0 on success and negative on failure, with a human `msg`. -1 "no plan working", -2 for the global params error.
- `phi` fields are radians; `RTKMSG.heading` is degrees.

## Still open

`cmd_recharge` cmd value, `plan_feedback` wire casing, `BatteryMSG.status` values while driving, what the robot publishes on controller theft, whether both brokers accept simultaneous sessions, and TLS on 8883.
