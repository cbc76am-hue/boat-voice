#!/usr/bin/env python3
"""Synthetic Signal K data → MQTT for Tolly dev/demo.

Publishes plausible values to the 6 SK topics HA's configuration.yaml subscribes
to, with a smooth random walk so Tolly's responses sound like a real boat
sitting at Shelter Bay.

Run via the boat-voice-sim.service systemd user unit, or by hand:
    python3 /home/boat/scripts/tolly_sensor_sim.py
"""
from __future__ import annotations

import math
import random
import signal
import subprocess
import sys
import time

BROKER = "localhost"
PORT = 1883
INTERVAL_S = 2.0  # how often to publish

# (topic, initial_value, step_size, min_clip, max_clip, formatter)
# Units match HA's value_template expectations:
#   SOG  m/s   (HA converts to mph via device_class=speed → user-prefs)
#   depth m   (HA → ft)
#   water_temp K  (HA template subtracts 273.15)
#   wind_speed m/s
#   wind_angle rad (HA template multiplies by 57.2958 for degrees)
#   battery V
state = {
    "vessels/self/navigation/speedOverGround":          {"v": 0.4,    "step": 0.05, "lo": 0.0,   "hi": 3.5},
    "vessels/self/environment/depth/belowKeel":         {"v": 4.2,    "step": 0.10, "lo": 1.5,   "hi": 8.0},
    "vessels/self/environment/water/temperature":       {"v": 285.0,  "step": 0.05, "lo": 283.0, "hi": 287.5},
    "vessels/self/environment/wind/speedApparent":      {"v": 3.5,    "step": 0.30, "lo": 0.0,   "hi": 15.0},
    "vessels/self/environment/wind/angleApparent":      {"v": 1.7,    "step": 0.05, "lo": 0.0,   "hi": 2 * math.pi},
    "vessels/self/electrical/batteries/house/voltage":  {"v": 12.8,   "step": 0.02, "lo": 12.3,  "hi": 13.6},
}

_running = True


def _stop(*_):
    global _running
    _running = False


def _walk(s: dict) -> float:
    drift = random.uniform(-s["step"], s["step"])
    s["v"] = max(s["lo"], min(s["hi"], s["v"] + drift))
    return s["v"]


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"tolly_sensor_sim: publishing to {BROKER}:{PORT} every {INTERVAL_S}s", flush=True)
    while _running:
        for topic, s in state.items():
            val = _walk(s)
            try:
                subprocess.run(
                    ["mosquitto_pub", "-h", BROKER, "-p", str(PORT), "-t", topic, "-m", f"{val:.4f}"],
                    check=True,
                    capture_output=True,
                    timeout=3,
                )
            except subprocess.CalledProcessError as err:
                print(f"pub fail {topic}: {err.stderr.decode().strip()}", flush=True)
            except subprocess.TimeoutExpired:
                print(f"pub timeout {topic}", flush=True)
        time.sleep(INTERVAL_S)
    print("tolly_sensor_sim: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
