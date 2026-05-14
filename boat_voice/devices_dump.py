"""python -m boat_voice.devices_dump — show audio devices PortAudio sees."""
from __future__ import annotations

import sounddevice as sd


def main() -> None:
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) <= 0 and d.get("max_output_channels", 0) <= 0:
            continue
        print(
            f"  [{i}] {d['name']!r:60s} "
            f"in={d['max_input_channels']} out={d['max_output_channels']} "
            f"sr={d['default_samplerate']}"
        )


if __name__ == "__main__":
    main()
