#!/usr/bin/env python3
"""Synthetic Mercury SmartCraft CAN-P traffic for Tolly dev/demo.

Publishes plausible engine data for both engines (source addresses 0x00 = port,
0x01 = starboard) as NMEA 2000 PGNs on a SocketCAN interface. Random-walks the
values so Tolly's responses sound like a real boat running, not a static fixture.

PGNs implemented:
  127488 Engine Parameters Rapid (single frame): RPM, boost, tilt
  127489 Engine Parameters Dynamic (Fast Packet): coolant, oil pressure/temp,
         alt voltage, fuel rate, runtime hours, engine load, torque

Usage:
  python3 tolly_engine_can_sim.py [INTERFACE]    # default vcan0

Runs via the boat-voice-engine-sim.service systemd user unit. Stop it when the
real RH-02 Pro lands and the SK provider gets pointed at can0.
"""
from __future__ import annotations

import math
import random
import signal
import socket
import struct
import sys
import time

INTERFACE = sys.argv[1] if len(sys.argv) > 1 else "vcan0"

CAN_RAW = 1
SOL_CAN_RAW = 101
CAN_RAW_FILTER = 1
CAN_EFF_FLAG = 0x80000000  # extended (29-bit) frame
ENGINE_SOURCES = {"port": 0x00, "starboard": 0x01}


def can_id(priority: int, pgn: int, source: int) -> int:
    """Build 29-bit J1939 CAN ID. PGN is 18 bits: bit 16 is the DP (data page),
    bits 8-15 are PDU format, bits 0-7 are PDU specific. NMEA 2000 engine PGNs
    127488/127489 = 0x1F200/0x1F201 — DP=1, PDU format=0xF2."""
    pdu_format = (pgn >> 8) & 0xFF
    pdu_specific = pgn & 0xFF
    dp = (pgn >> 16) & 0x1
    return (
        ((priority & 0x7) << 26)
        | (dp << 24)
        | (pdu_format << 16)
        | (pdu_specific << 8)
        | (source & 0xFF)
        | CAN_EFF_FLAG
    )


def open_can(interface: str) -> socket.socket:
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, CAN_RAW)
    s.bind((interface,))
    return s


def send_frame(s: socket.socket, can_id_val: int, data: bytes) -> None:
    """Send a single CAN frame (up to 8 bytes payload)."""
    assert len(data) <= 8
    pad = data + b"\xff" * (8 - len(data))
    frame = struct.pack("=IB3x8s", can_id_val, len(data), pad)
    s.send(frame)


def send_fast_packet(s: socket.socket, pgn: int, source: int, payload: bytes, priority: int = 3) -> None:
    """Send an NMEA 2000 Fast Packet PGN (>8 bytes). Sequence counter rolls 0..7."""
    if not hasattr(send_fast_packet, "_seq"):
        send_fast_packet._seq = 0  # type: ignore
    seq = send_fast_packet._seq  # type: ignore
    send_fast_packet._seq = (seq + 1) % 8  # type: ignore

    cid = can_id(priority, pgn, source)
    total_len = len(payload)
    # Frame 0: [seq<<5 | 0][total_len][6 bytes of payload]
    frame0 = bytes([(seq << 5) | 0, total_len]) + payload[:6]
    send_frame(s, cid, frame0)
    # Subsequent frames: [seq<<5 | frame#][7 bytes of payload]
    pos = 6
    frame_num = 1
    while pos < total_len:
        chunk = payload[pos:pos + 7]
        chunk = chunk + b"\xff" * (7 - len(chunk))
        frame = bytes([(seq << 5) | frame_num]) + chunk
        send_frame(s, cid, frame)
        pos += 7
        frame_num += 1
        time.sleep(0.005)  # small gap so receiver can sequence


# (initial_value, step_size, min, max) per source
state = {
    "port": {
        "rpm": [820.0, 30, 600, 4200],          # idle around 820, can rev up
        "coolant_K": [354.0, 0.3, 343, 365],    # 80–92 C
        "oil_pressure_Pa": [310000, 5000, 200000, 450000],  # 200-450 kPa
        "oil_temp_K": [358.0, 0.3, 345, 380],
        "alt_V": [13.8, 0.05, 13.0, 14.4],
        "fuel_L_per_s": [0.0008, 0.0001, 0.0003, 0.005],
        "hours_s": [365 * 3600, 0, 0, 1e9],     # 365 engine hours
        "load_frac": [0.20, 0.02, 0.05, 0.95],
    },
    "starboard": {
        "rpm": [840.0, 30, 600, 4200],
        "coolant_K": [355.5, 0.3, 343, 365],
        "oil_pressure_Pa": [305000, 5000, 200000, 450000],
        "oil_temp_K": [359.0, 0.3, 345, 380],
        "alt_V": [13.9, 0.05, 13.0, 14.4],
        "fuel_L_per_s": [0.0008, 0.0001, 0.0003, 0.005],
        "hours_s": [382 * 3600, 0, 0, 1e9],
        "load_frac": [0.22, 0.02, 0.05, 0.95],
    },
}


def walk(s: dict, key: str) -> float:
    v, step, lo, hi = s[key]
    drift = random.uniform(-step, step)
    new = max(lo, min(hi, v + drift))
    s[key][0] = new
    return new


def encode_127488(rpm: float, instance: int) -> bytes:
    """PGN 127488 Engine Rapid: instance(1), RPM*4(2 LE), boost(2 LE), tilt(1)+reserved(2)."""
    rpm_raw = int(rpm * 4)  # NMEA 2000 stores as 0.25 RPM units
    return struct.pack("<BHHb", instance, rpm_raw, 0xFFFF, -128) + b"\xff\xff"


def encode_127489(s: dict, instance: int) -> bytes:
    """PGN 127489 Engine Dynamic — 26-byte fast-packet payload.
    Layout (NMEA 2000):
       instance              uint8       0
       oil_pressure          uint16 LE   100 Pa/bit
       oil_temp              uint16 LE   0.1 K/bit
       coolant_temp          uint16 LE   0.01 K/bit
       alternator_potential  int16  LE   0.01 V/bit
       fuel_rate             int16  LE   0.1 L/h/bit
       engine_hours          uint32 LE   1 s/bit
       coolant_pressure      uint16 LE   100 Pa/bit
       fuel_pressure         int16  LE   1000 Pa/bit
       reserved              uint8
       discrete_status1      uint16 LE
       discrete_status2      uint16 LE
       engine_load           int8        1%/bit
       engine_torque         int8        1%/bit
    """
    oil_p = int(s["oil_pressure_Pa"][0] / 100)
    oil_t = int(s["oil_temp_K"][0] * 10)
    coolant_t = int(s["coolant_K"][0] * 100)
    alt_v = int(s["alt_V"][0] * 100)
    fuel_rate = int(s["fuel_L_per_s"][0] * 3600 * 10)  # L/h * 10
    hours = int(s["hours_s"][0])
    coolant_p = 0xFFFF
    fuel_p = 0x7FFF
    status1 = 0
    status2 = 0
    load = int(s["load_frac"][0] * 100)
    torque = load  # approximation; real Mercury reports separately

    return struct.pack(
        "<BHHHhhIHhBHHbb",
        instance,
        oil_p,
        oil_t,
        coolant_t,
        alt_v,
        fuel_rate,
        hours,
        coolant_p,
        fuel_p,
        0xFF,
        status1,
        status2,
        load,
        torque,
    )


_running = True


def _stop(*_):
    global _running
    _running = False


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"tolly_engine_can_sim: injecting on {INTERFACE}", flush=True)
    s = open_can(INTERFACE)

    last_dynamic = 0.0
    while _running:
        now = time.monotonic()
        # PGN 127488 — every 100ms each engine
        for side, src in ENGINE_SOURCES.items():
            rpm = walk(state[side], "rpm")
            payload = encode_127488(rpm, instance=0 if side == "port" else 1)
            send_frame(s, can_id(2, 127488, src), payload)

        # PGN 127489 — every 500ms
        if now - last_dynamic > 0.5:
            for side, src in ENGINE_SOURCES.items():
                for key in ("coolant_K", "oil_pressure_Pa", "oil_temp_K", "alt_V", "fuel_L_per_s", "load_frac"):
                    walk(state[side], key)
                payload = encode_127489(state[side], instance=0 if side == "port" else 1)
                send_fast_packet(s, 127489, src, payload)
            last_dynamic = now

        time.sleep(0.1)

    s.close()
    print("tolly_engine_can_sim: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
