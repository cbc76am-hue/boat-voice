#!/usr/bin/env python3
"""Synthetic NMEA 0183 nav data into Signal K (dev/demo).

Replaces the older `tolly_sensor_sim.py` (which wrote directly to MQTT,
bypassing SK). This script emits NMEA 0183 sentences over UDP to SK's
configured NMEA0183 listener (port 10120). SK parses them into the standard
SK paths, which then flow through:
  - signalk-mqtt-gw  ->  HA mqtt: sensors
  - SK comm driver in OpenCPN  ->  Dashboard plugin

Sentences emitted (every second):
  $GPRMC  position, speed, course
  $WIMWV  wind angle + speed (apparent)
  $IIDBT  depth below transducer
  $IIMTW  water temperature

Battery voltage takes a different path: SK's standard NMEA 0183 hooks don't
parse XDR sentences, so we push electrical.batteries.house.voltage as a native
SK delta over WebSocket instead. Token at ~/.config/tolly-nav-sim/sk-token.
"""
from __future__ import annotations

import json
import math
import os
import random
import signal
import socket
import sys
import time
from datetime import datetime, timezone

try:
    import websocket  # python3-websocket (websocket-client 1.7.0)
except ImportError:
    websocket = None

DEST = ("127.0.0.1", 10120)
SK_WS_URL = "ws://127.0.0.1:3000/signalk/v1/stream?subscribe=none"
SK_TOKEN_FILE = os.path.expanduser("~/.config/tolly-nav-sim/sk-token")


def nmea_checksum(body: str) -> str:
    cs = 0
    for c in body:
        cs ^= ord(c)
    return f"{cs:02X}"


def nmea(body: str) -> bytes:
    return f"${body}*{nmea_checksum(body)}\r\n".encode("ascii")


# walking-state — initial, step, lo, hi
state = {
    "sog_kt":         {"v": 0.5,    "step": 0.1,   "lo": 0.0,   "hi": 5.0},
    "cog_deg":        {"v": 175.0,  "step": 2.0,   "lo": 0.0,   "hi": 359.0},  # heading
    "depth_ft":       {"v": 14.0,   "step": 0.5,   "lo": 5.0,   "hi": 40.0},
    "water_temp_C":   {"v": 11.5,   "step": 0.05,  "lo": 9.5,   "hi": 13.5},
    "wind_angle_deg": {"v": 95.0,   "step": 4.0,   "lo": 0.0,   "hi": 359.0},
    "wind_speed_kt":  {"v": 7.0,    "step": 0.5,   "lo": 0.0,   "hi": 25.0},
    "house_v":        {"v": 12.8,   "step": 0.02,  "lo": 12.3,  "hi": 13.6},
}
# Home port near Shelter Bay, La Conner WA — dock position
# HOME — just SW of the C08 Shelter Bay Marina slip, in the open water at
# the south mouth of the Swinomish Channel (Skagit Bay opening).  Picked
# this offset because the actual slip coordinate lands inside the marine-
# router's 50 m no-go raster (Padilla Bay's nav basin is severed from the
# main Salish Sea at chart resolution), so route planning from the slip
# itself returns "disconnected basin".  This point is the closest in-main-
# component cell to the slip (1.37 nm away) — a realistic "just left the
# slip, heading out" position for testing PlanRoute end-to-end.
LAT_DEG = 48.38979            # 48 23.39' N
LON_DEG = -122.54648          # 122 32.79' W


def walk(key: str) -> float:
    s = state[key]
    drift = random.uniform(-s["step"], s["step"])
    s["v"] = max(s["lo"], min(s["hi"], s["v"] + drift))
    return s["v"]


def fmt_lat(lat: float) -> tuple[str, str]:
    h = "N" if lat >= 0 else "S"
    lat = abs(lat)
    deg = int(lat)
    minutes = (lat - deg) * 60
    return f"{deg:02d}{minutes:07.4f}", h


def fmt_lon(lon: float) -> tuple[str, str]:
    h = "E" if lon >= 0 else "W"
    lon = abs(lon)
    deg = int(lon)
    minutes = (lon - deg) * 60
    return f"{deg:03d}{minutes:07.4f}", h


def rmc(sog_kt: float, cog_deg: float) -> bytes:
    now = datetime.now(timezone.utc)
    hhmmss = now.strftime("%H%M%S.00")
    ddmmyy = now.strftime("%d%m%y")
    lat_s, lat_h = fmt_lat(LAT_DEG)
    lon_s, lon_h = fmt_lon(LON_DEG)
    body = f"GPRMC,{hhmmss},A,{lat_s},{lat_h},{lon_s},{lon_h},{sog_kt:.1f},{cog_deg:.1f},{ddmmyy},,,A"
    return nmea(body)


def mwv(angle_deg: float, speed_kt: float) -> bytes:
    body = f"WIMWV,{angle_deg:.1f},R,{speed_kt:.1f},N,A"
    return nmea(body)


def dbt(depth_ft: float) -> bytes:
    depth_m = depth_ft * 0.3048
    depth_fa = depth_ft / 6.0
    body = f"IIDBT,{depth_ft:.1f},f,{depth_m:.1f},M,{depth_fa:.1f},F"
    return nmea(body)


def mtw(temp_c: float) -> bytes:
    body = f"IIMTW,{temp_c:.1f},C"
    return nmea(body)


class SkDeltaPublisher:
    """Maintains a SK WebSocket connection and pushes deltas. Reconnects on
    failure. Silently no-ops if the websocket library or token is missing —
    callers can keep emitting NMEA without knowing SK is down."""

    def __init__(self, url: str, token_path: str):
        self.url = url
        self.token = self._load_token(token_path)
        self.ws: websocket.WebSocket | None = None

    @staticmethod
    def _load_token(path: str) -> str | None:
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError as e:
            print(f"sk: token load fail ({e}); SK pushes disabled", flush=True)
            return None

    def _connect(self) -> bool:
        if websocket is None or self.token is None:
            return False
        try:
            self.ws = websocket.create_connection(
                self.url,
                header=[f"Authorization: Bearer {self.token}"],
                timeout=3,
            )
            print(f"sk: connected {self.url}", flush=True)
            return True
        except Exception as e:
            print(f"sk: connect fail ({e})", flush=True)
            self.ws = None
            return False

    def send(self, path: str, value: float) -> None:
        if self.ws is None and not self._connect():
            return
        delta = {
            "updates": [
                {
                    "$source": "tolly-nav-sim",
                    "values": [{"path": path, "value": value}],
                }
            ]
        }
        try:
            self.ws.send(json.dumps(delta))
        except Exception as e:
            print(f"sk: send fail ({e}); will reconnect", flush=True)
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


_running = True


def _stop(*_):
    global _running
    _running = False


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"tolly_nav_sim: emitting NMEA 0183 to udp://{DEST[0]}:{DEST[1]}", flush=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk = SkDeltaPublisher(SK_WS_URL, SK_TOKEN_FILE)

    while _running:
        sentences = [
            rmc(walk("sog_kt"), walk("cog_deg")),
            mwv(walk("wind_angle_deg"), walk("wind_speed_kt")),
            dbt(walk("depth_ft")),
            mtw(walk("water_temp_C")),
        ]
        for sentence in sentences:
            try:
                s.sendto(sentence, DEST)
            except OSError as err:
                print(f"send fail: {err}", flush=True)
        sk.send("electrical.batteries.house.voltage", round(walk("house_v"), 2))
        time.sleep(1.0)

    s.close()
    sk.close()
    print("tolly_nav_sim: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
