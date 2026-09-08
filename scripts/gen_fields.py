"""Generate protocol/fields.yaml from fixtures plus a curated seed of known semantics.

Usage: uv run python scripts/gen_fields.py [--fixtures protocol/fixtures] [--out protocol/fields.yaml]

Every flattened DeviceMSG path observed in any fixture (push frames and
get_device_msg snapshots) gets an entry with the observed JSON types, an
example value, and how often it appeared in each source. Paths present in
SEED get their curated semantics merged on top: unit, scale, sentinel, the
Home Assistant entity they should become, and notes. Everything else is a
candidate with no entity until someone documents it.

The seed is the curated knowledge from the vendor SDK field table, the
briangann fork's live inventory, and our own captures. Keep it small and
honest: an entry here is a claim that the fixtures back up.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from yarbo_local import codec

# path -> curated semantics. Keys: entity, device_class, unit, scale, sentinel,
# enabled, category, codes (reference into codes.yaml), notes, source.
SEED: dict[str, dict[str, Any]] = {
    # --- battery
    "BatteryMSG.capacity": {
        "entity": "sensor",
        "device_class": "battery",
        "unit": "%",
        "enabled": True,
        "notes": "Vendor SDK rescales 90..95 to 90..100 because the firmware tops out at 95 on some units; ours reads 100 at full.",
        "source": "vendor SDK",
    },
    "BatteryMSG.health": {"entity": "sensor", "unit": "%", "enabled": True, "source": "vendor SDK"},
    "BatteryMSG.voltage": {
        "entity": "sensor",
        "device_class": "voltage",
        "unit": "V",
        "scale": 0.001,
        "enabled": False,
        "notes": "Reported in mV (41000 while docked).",
        "source": "capture",
    },
    "BatteryMSG.current": {
        "entity": "sensor",
        "device_class": "current",
        "unit": "A",
        "scale": 0.001,
        "enabled": False,
        "notes": "Reported in mA; negative while charging (-300 docked).",
        "source": "capture",
    },
    "BatteryMSG.status": {
        "entity": "binary_sensor",
        "device_class": "battery_charging",
        "enabled": True,
        "codes": "BatteryMSG.status",
        "notes": "Vendor rule: > 1 means charging on the wireless dock. Observed 1 while on the wired charger.",
        "source": "vendor SDK",
    },
    "BatteryMSG.temp_err": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": True,
        "source": "vendor SDK",
    },
    "BatteryMSG.heating_status": {
        "entity": "binary_sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    **{
        f"BatteryMSG.temperature{i}": {
            "entity": "sensor",
            "device_class": "temperature",
            "unit": "°C",
            "enabled": False,
            "category": "diagnostic",
            "source": "vendor SDK",
        }
        for i in range(1, 7)
    },
    # --- state machine
    "StateMSG.working_state": {
        "entity": "binary_sensor",
        "enabled": True,
        "codes": "heart_beat.working_state",
        "notes": "0 asleep, 1 awake. Same field as heart_beat.working_state.",
        "source": "capture",
    },
    "StateMSG.on_going_planning": {
        "entity": "sensor",
        "device_class": "enum",
        "enabled": True,
        "codes": "StateMSG.on_going_planning",
        "source": "vendor SDK",
    },
    "StateMSG.planning_paused": {
        "entity": "sensor",
        "device_class": "enum",
        "enabled": True,
        "codes": "StateMSG.planning_paused",
        "source": "vendor SDK",
    },
    "StateMSG.on_going_recharging": {
        "entity": "sensor",
        "device_class": "enum",
        "enabled": True,
        "codes": "StateMSG.on_going_recharging",
        "source": "vendor SDK",
    },
    "StateMSG.on_going_to_start_point": {
        "entity": "binary_sensor",
        "enabled": False,
        "source": "community",
    },
    "StateMSG.error_code": {
        "entity": "sensor",
        "enabled": True,
        "notes": "0 is no error. Non-zero codes are not yet mapped.",
        "source": "vendor SDK",
    },
    "StateMSG.charging_status": {
        "entity": "binary_sensor",
        "device_class": "battery_charging",
        "enabled": True,
        "notes": "Community rule: 1..3 charging. Observed 0 on the wired charger, so BatteryMSG.status and BodyMsg.recharge_state are the reliable sources.",
        "source": "community",
    },
    "StateMSG.machine_controller": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "notes": "Reads 1 idle with no app connected. Semantics open (Phase 0 question 9).",
        "source": "capture",
    },
    "StateMSG.car_controller": {
        "entity": "binary_sensor",
        "enabled": False,
        "category": "diagnostic",
        "notes": "Manual controller (gamepad) active.",
        "source": "community",
    },
    "StateMSG.person_detect_status": {
        "entity": "switch",
        "enabled": True,
        "notes": "Read-back for set_person_detect.",
        "source": "vendor SDK",
    },
    "StateMSG.child_lock_status": {
        "entity": "switch",
        "enabled": True,
        "notes": "Read-back for set_child_lock.",
        "source": "vendor SDK",
    },
    "StateMSG.enable_sound": {
        "entity": "switch",
        "enabled": True,
        "notes": "Read-back for set_sound_param.enable.",
        "source": "vendor SDK",
    },
    "StateMSG.volume": {
        "entity": "number",
        "unit": "%",
        "scale": 100,
        "enabled": True,
        "notes": "Float 0..1 on the wire; set_sound_param.vol uses the same scale.",
        "source": "capture",
    },
    "StateMSG.robot_follow_state": {
        "entity": "switch",
        "enabled": False,
        "notes": "Read-back for set_follow_state.",
        "source": "vendor SDK",
    },
    "StateMSG.stuck": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": True,
        "source": "vendor SDK",
    },
    "StateMSG.obstacle": {"entity": "binary_sensor", "enabled": False, "source": "vendor SDK"},
    "StateMSG.obstacle_toggle": {
        "entity": "switch",
        "enabled": False,
        "notes": "Candidate read-back for set_map_obstacle_switch.",
        "source": "capture",
    },
    "StateMSG.lidar_switch": {
        "entity": "switch",
        "enabled": False,
        "category": "config",
        "source": "capture",
    },
    "StateMSG.lidar_state": {
        "entity": "binary_sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    "StateMSG.lidar_smudge": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": False,
        "source": "capture",
    },
    "StateMSG.en_state_led": {
        "entity": "switch",
        "enabled": False,
        "category": "config",
        "source": "capture",
    },
    "StateMSG.en_warn_led": {
        "entity": "switch",
        "enabled": False,
        "category": "config",
        "source": "capture",
    },
    "StateMSG.sentry_mode_status": {
        "entity": "binary_sensor",
        "enabled": False,
        "source": "capture",
    },
    "StateMSG.shipping_mode": {
        "entity": "binary_sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    "StateMSG.is_pedestrian": {
        "entity": "binary_sensor",
        "device_class": "occupancy",
        "enabled": True,
        "source": "capture",
    },
    "StateMSG.is_animal": {
        "entity": "binary_sensor",
        "device_class": "occupancy",
        "enabled": True,
        "source": "capture",
    },
    "StateMSG.schedule_state": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    "StateMSG.schedule_id": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    "StateMSG.self_check_status": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    "StateMSG.need_back_to_ds": {"entity": "binary_sensor", "enabled": False, "source": "capture"},
    "StateMSG.voice_locale": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    # --- RTK / position
    "RTKMSG.status": {
        "entity": "sensor",
        "device_class": "enum",
        "enabled": True,
        "codes": "RTKMSG.status",
        "notes": "String on the wire. Observed '1' asleep and '5' awake.",
        "source": "vendor SDK",
    },
    "RTKMSG.statush": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "notes": "Heading solution status, string.",
        "source": "capture",
    },
    "RTKMSG.sat_num": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "vendor SDK",
    },
    "RTKMSG.heading": {
        "entity": "sensor",
        "unit": "°",
        "enabled": False,
        "notes": "Degrees, compass.",
        "source": "capture",
    },
    "RTKMSG.heading_status": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "RTKMSG.heading_dop": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "RTKMSG.gga_atn_dis": {
        "entity": "sensor",
        "unit": "m",
        "enabled": False,
        "category": "diagnostic",
        "notes": "Antenna baseline distance.",
        "source": "community",
    },
    "CombinedOdom.x": {
        "entity": "sensor",
        "unit": "m",
        "enabled": False,
        "notes": "Site frame, +x west per vendor conversion. Frame not yet verified by movement.",
        "source": "vendor SDK",
    },
    "CombinedOdom.y": {
        "entity": "sensor",
        "unit": "m",
        "enabled": False,
        "notes": "Site frame, +y north.",
        "source": "vendor SDK",
    },
    "CombinedOdom.phi": {
        "entity": "sensor",
        "unit": "rad",
        "enabled": False,
        "notes": "Radians; dock straightPhi is in the same unit.",
        "source": "capture",
    },
    "combined_odom_confidence": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "rtk_base_data.rover.gngga": {
        "entity": "device_tracker",
        "enabled": True,
        "notes": "NMEA GGA; lat/lon, fix quality (4 RTK fixed, 5 float), satellites, HDOP, altitude.",
        "source": "community",
    },
    "rtk_base_data.base.gngga": {
        "entity": None,
        "notes": "Base station GGA; fix quality 7 observed (fixed/manual position).",
        "source": "capture",
    },
    "rtcm_age": {
        "entity": "sensor",
        "unit": "s",
        "enabled": False,
        "category": "diagnostic",
        "sentinel": -1.0,
        "source": "community",
    },
    "rtcm_info.current_source_type": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    # --- head / versions
    "HeadMsg.head_type": {
        "entity": "sensor",
        "device_class": "enum",
        "enabled": True,
        "codes": "HeadMsg.head_type",
        "source": "vendor SDK",
    },
    "HeadSerialMsg.head_sn": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "notes": "Head device serial; redacted in fixtures.",
        "source": "vendor SDK",
    },
    "HeadAndVersionCheck.head_firmware_version": {
        "entity": "update",
        "enabled": True,
        "source": "vendor SDK",
    },
    "BodyVersionMsg.body_fw_version": {
        "entity": "update",
        "enabled": True,
        "notes": "Body MCU firmware (3.2.26 observed), not the main firmware.",
        "source": "vendor SDK",
    },
    "version": {
        "entity": "update",
        "enabled": True,
        "notes": "Main firmware. Drives the zlib rule (>= 3.9.0). 3.14.11 observed.",
        "source": "vendor SDK",
    },
    "ble_version": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    "dc_version": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "notes": "Base station firmware (1.1.11 observed).",
        "source": "capture",
    },
    "chassis_version_msg.firmware_version": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    "min_app_version": {"entity": None, "notes": "3.9.0 observed.", "source": "capture"},
    # --- body / faults
    "BodyMsg.recharge_state": {
        "entity": "binary_sensor",
        "device_class": "plug",
        "enabled": True,
        "codes": "BodyMsg.rechargeState",
        "notes": "1 and 3 mean wired charging, which blocks plan start. Observed 3 on the wired charger.",
        "source": "vendor SDK",
    },
    "BodyMsg.body_stop_button_state": {
        "entity": "binary_sensor",
        "device_class": "safety",
        "enabled": True,
        "source": "community",
    },
    "BodyMsg.external_button_state": {
        "entity": "binary_sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "BodyMsg.left_wheel_fault_state": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": False,
        "source": "community",
    },
    "BodyMsg.right_wheel_fault_state": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": False,
        "source": "community",
    },
    "BodyMsg.power_fault_state": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": False,
        "source": "capture",
    },
    "BodyMsg.push_pod_fault_state": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": False,
        "source": "capture",
    },
    "abnormal_msg.error_code": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "abnormal_msg.left_motor_err": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": False,
        "source": "briangann",
    },
    "abnormal_msg.right_motor_err": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": False,
        "source": "briangann",
    },
    "abnormal_msg.radar_state": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": False,
        "source": "briangann",
    },
    "abnormal_msg.power_fault": {
        "entity": "binary_sensor",
        "device_class": "problem",
        "enabled": False,
        "sentinel": -1,
        "source": "briangann",
    },
    # --- electrics / thermal
    "EletricMSG.body_ambient_ntc_temp": {
        "entity": "sensor",
        "device_class": "temperature",
        "unit": "°C",
        "enabled": True,
        "source": "vendor SDK",
    },
    "EletricMSG.mos_temp": {
        "entity": "sensor",
        "device_class": "temperature",
        "unit": "°C",
        "enabled": False,
        "category": "diagnostic",
        "sentinel": -40.0,
        "source": "briangann",
    },
    "EletricMSG.ntc_temperature": {
        "entity": "sensor",
        "device_class": "temperature",
        "unit": "°C",
        "enabled": False,
        "category": "diagnostic",
        "sentinel": -40.0,
        "source": "briangann",
    },
    "EletricMSG.lwheel_current": {
        "entity": "sensor",
        "device_class": "current",
        "unit": "A",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    "EletricMSG.rwheel_current": {
        "entity": "sensor",
        "device_class": "current",
        "unit": "A",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    "EletricMSG.brushless_motor_current": {
        "entity": "sensor",
        "device_class": "current",
        "unit": "A",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    "EletricMSG.push_pod_current": {
        "entity": "sensor",
        "device_class": "current",
        "unit": "A",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    # --- running status / environment
    "RunningStatusMSG.rain_sensor_data": {
        "entity": "sensor",
        "enabled": False,
        "notes": "Raw; 0 dry.",
        "source": "vendor SDK",
    },
    "RunningStatusMSG.chute_angle": {
        "entity": "sensor",
        "unit": "°",
        "enabled": False,
        "notes": "Snow blower head only.",
        "source": "vendor SDK",
    },
    "RunningStatusMSG.head_gyro_pitch": {
        "entity": "sensor",
        "unit": "°",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "RunningStatusMSG.head_gyro_roll": {
        "entity": "sensor",
        "unit": "°",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "RunningStatusMSG.impact_sensor": {
        "entity": "binary_sensor",
        "enabled": False,
        "source": "briangann",
    },
    "ultrasonic_msg.lf_dis": {
        "entity": "sensor",
        "device_class": "distance",
        "unit": "mm",
        "enabled": False,
        "sentinel": 9999,
        "notes": "9999 means clear. 0 observed while asleep.",
        "source": "briangann",
    },
    "ultrasonic_msg.mt_dis": {
        "entity": "sensor",
        "device_class": "distance",
        "unit": "mm",
        "enabled": False,
        "sentinel": 9999,
        "source": "briangann",
    },
    "ultrasonic_msg.rf_dis": {
        "entity": "sensor",
        "device_class": "distance",
        "unit": "mm",
        "enabled": False,
        "sentinel": 9999,
        "source": "briangann",
    },
    "WheelSpeedMSG.left": {
        "entity": "sensor",
        "unit": "m/s",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    "WheelSpeedMSG.right": {
        "entity": "sensor",
        "unit": "m/s",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    "WheelSpeedMSG.dist_left": {
        "entity": "sensor",
        "device_class": "distance",
        "unit": "m",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    "WheelSpeedMSG.dist_right": {
        "entity": "sensor",
        "device_class": "distance",
        "unit": "m",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    # --- network
    "route_priority.hg0": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "notes": "HaLow route metric; lowest wins. 10 observed.",
        "source": "community",
    },
    "route_priority.wlan0": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "notes": "Wi-Fi route metric. 600 observed.",
        "source": "community",
    },
    "route_priority.wwan0": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "sentinel": -1,
        "notes": "-1 when no LTE module.",
        "source": "community",
    },
    "halow_status.strength": {
        "entity": "sensor",
        "device_class": "signal_strength",
        "unit": "dBm",
        "enabled": True,
        "source": "vendor SDK",
    },
    "halow_status.conn_status": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "notes": "9 observed while connected.",
        "source": "capture",
    },
    "halow_status.ssid": {
        "entity": None,
        "notes": "Base station HaLow SSID equals base_name; redacted.",
        "source": "capture",
    },
    "net_module_status.lte_rssi": {
        "entity": "sensor",
        "device_class": "signal_strength",
        "unit": "dBm",
        "enabled": False,
        "sentinel": 0,
        "source": "vendor SDK",
    },
    "net_module_status.lte_iccid": {
        "entity": None,
        "notes": "SIM identifier; redacted, never expose.",
        "source": "capture",
    },
    "NetMSG.mqtt_server": {
        "entity": "binary_sensor",
        "device_class": "connectivity",
        "enabled": False,
        "category": "diagnostic",
        "notes": "Cloud MQTT reachability from the robot's view. 0 observed on an egress-blocked network is expected.",
        "source": "community",
    },
    "NetMSG.ntrip_service": {
        "entity": "binary_sensor",
        "device_class": "connectivity",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "NetMSG.dns": {
        "entity": "binary_sensor",
        "device_class": "connectivity",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "base_status": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "notes": "0 asleep, 7 awake observed; semantics open.",
        "source": "capture",
    },
    "base_name": {
        "entity": None,
        "notes": "Base station identifier; redacted.",
        "source": "capture",
    },
    "ntrip_server_type": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    # --- charging dock
    "wireless_recharge.state": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "wireless_recharge.error_code": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "community",
    },
    "wireless_recharge.output_voltage": {
        "entity": "sensor",
        "device_class": "voltage",
        "unit": "V",
        "enabled": False,
        "category": "diagnostic",
        "notes": "briangann divides by 1000 when > 1000; 15 observed raw.",
        "source": "briangann",
    },
    "wireless_recharge.output_current": {
        "entity": "sensor",
        "device_class": "current",
        "unit": "A",
        "enabled": False,
        "category": "diagnostic",
        "notes": "53 observed raw; scale unresolved.",
        "source": "briangann",
    },
    "wireless_recharge.temperature": {
        "entity": "sensor",
        "device_class": "temperature",
        "unit": "°C",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    # --- mower head telemetry (head types 3 and 5)
    "mower_head_info01.rain_sensor": {
        "entity": "binary_sensor",
        "device_class": "moisture",
        "enabled": True,
        "heads": [3, 5],
        "source": "vendor SDK",
    },
    "mower_head_info03.left_blade_motor_current": {
        "entity": "sensor",
        "device_class": "current",
        "unit": "A",
        "scale": 0.01,
        "enabled": False,
        "heads": [3, 5],
        "source": "briangann",
    },
    "mower_head_info04.right_blade_motor_current": {
        "entity": "sensor",
        "device_class": "current",
        "unit": "A",
        "scale": 0.01,
        "enabled": False,
        "heads": [3, 5],
        "source": "briangann",
    },
    "mower_head_info03.left_blade_motor_rpm": {
        "entity": "sensor",
        "unit": "rpm",
        "enabled": False,
        "heads": [3, 5],
        "notes": "Negative means counter-clockwise.",
        "source": "briangann",
    },
    "mower_head_info04.right_blade_motor_rpm": {
        "entity": "sensor",
        "unit": "rpm",
        "enabled": False,
        "heads": [3, 5],
        "source": "briangann",
    },
    "mower_head_info03.left_blade_motor_speed": {
        "entity": "sensor",
        "unit": "%",
        "enabled": False,
        "heads": [3, 5],
        "source": "briangann",
    },
    "mower_head_info04.right_blade_motor_speed": {
        "entity": "sensor",
        "unit": "%",
        "enabled": False,
        "heads": [3, 5],
        "source": "briangann",
    },
    # --- lights
    **{
        f"LedInfoMSG.{k}": {
            "entity": "light",
            "enabled": False,
            "notes": "0..255; read-back for light_ctrl.",
            "source": "capture",
        }
        for k in ("led_head", "led_left_w", "led_right_w")
    },
    # --- system / software
    "SoftwareUpdate.update_status": {
        "entity": "binary_sensor",
        "device_class": "update",
        "enabled": True,
        "notes": "Non-zero while an OTA is in progress.",
        "source": "capture",
    },
    "SoftwareUpdate.download_size": {
        "entity": "sensor",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    "SoftwareUpdate.still_need_time": {
        "entity": "sensor",
        "unit": "s",
        "enabled": False,
        "category": "diagnostic",
        "source": "capture",
    },
    "system_info.cpu.Usage": {
        "entity": "sensor",
        "unit": "%",
        "enabled": False,
        "category": "diagnostic",
        "notes": "Key contains a dot; use a path-aware extractor.",
        "source": "briangann",
    },
    "system_info.cpu.Temperature": {
        "entity": "sensor",
        "device_class": "temperature",
        "unit": "°C",
        "enabled": False,
        "category": "diagnostic",
        "notes": "String with unit suffix, e.g. '44.384000 °C'.",
        "source": "capture",
    },
    "system_info.mem.MemAvailable": {
        "entity": "sensor",
        "unit": "kB",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    "system_info.userdata.disk.availableSize": {
        "entity": "sensor",
        "unit": "B",
        "enabled": False,
        "category": "diagnostic",
        "source": "briangann",
    },
    "timestamp": {"entity": None, "notes": "Epoch seconds of the frame.", "source": "capture"},
    "green_grass_update_switch": {
        "entity": "switch",
        "enabled": False,
        "category": "config",
        "notes": "Greengrass auto-update; forbidden to write.",
        "source": "community",
    },
    "ipcamera_ota_switch": {
        "entity": "switch",
        "enabled": False,
        "category": "config",
        "notes": "Camera OTA; forbidden to write.",
        "source": "community",
    },
}


def inventory(fixtures: Path) -> dict[str, dict[str, Any]]:
    paths: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"types": Counter(), "examples": [], "push": 0, "snapshot": 0}
    )

    def note(flat: dict[str, Any], kind: str) -> None:
        for key, value in flat.items():
            entry = paths[key]
            entry["types"][type(value).__name__] += 1
            entry[kind] += 1
            if len(entry["examples"]) < 2 and value not in entry["examples"]:
                entry["examples"].append(value)

    for path in sorted(fixtures.glob("*/*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            topic, payload = rec.get("topic", ""), rec.get("payload")
            if topic.endswith("/device/DeviceMSG") and isinstance(payload, dict):
                note(codec.flatten(payload), "push")
            elif (
                topic.endswith("/device/data_feedback")
                and isinstance(payload, dict)
                and payload.get("topic") == "get_device_msg"
                and isinstance(payload.get("data"), dict)
            ):
                note(codec.flatten(payload["data"]), "snapshot")
    return paths


def build(fixtures: Path) -> dict[str, Any]:
    inv = inventory(fixtures)
    fields: dict[str, Any] = {}
    for path in sorted(inv):
        obs = inv[path]
        entry: dict[str, Any] = {
            "types": sorted(obs["types"]),
            "example": obs["examples"][0] if obs["examples"] else None,
            "seen": {"push": obs["push"], "snapshot": obs["snapshot"]},
            "status": "candidate",
        }
        seed = SEED.get(path)
        if seed:
            entry.update({k: v for k, v in seed.items() if v is not None or k == "entity"})
            entry["status"] = "verified" if seed.get("source") == "capture" else "candidate"
        fields[path] = entry
    unseeded = [p for p in SEED if p not in inv]
    return {
        "version": 1,
        "generated_by": "scripts/gen_fields.py",
        "firmware_seen": ["3.14.11"],
        "summary": {
            "paths": len(fields),
            "seeded": sum(1 for p in fields if p in SEED),
            "seed_paths_not_in_fixtures": unseeded,
        },
        "fields": fields,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=Path, default=Path("protocol/fixtures"))
    ap.add_argument("--out", type=Path, default=Path("protocol/fields.yaml"))
    args = ap.parse_args()
    data = build(args.fixtures)
    header = (
        "# DeviceMSG field map. GENERATED by scripts/gen_fields.py from protocol/fixtures\n"
        "# plus the curated SEED table in that script. Edit the seed, not this file.\n"
        "#\n"
        "# status: verified means the semantics were confirmed on a capture; candidate\n"
        "# means the path was observed but its meaning comes from another project or is\n"
        "# unknown. entity is the Home Assistant platform it should become, or null.\n"
        "# heads lists the head types a field is meaningful for. scale multiplies the raw\n"
        "# value; sentinel is a raw value meaning 'no reading'.\n\n"
    )
    args.out.write_text(
        header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110),
        encoding="utf-8",
    )
    print(f"wrote {args.out}: {data['summary']['paths']} paths, {data['summary']['seeded']} seeded")
    if data["summary"]["seed_paths_not_in_fixtures"]:
        print("seed paths not present in fixtures:", data["summary"]["seed_paths_not_in_fixtures"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
