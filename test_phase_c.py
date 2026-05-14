"""Phase C smoke test: probe audio devices and play a 1-second 440 Hz tone."""
from __future__ import annotations

import asyncio
import numpy as np

from boat_voice.audio import (
    MicStream,
    SpeakerSink,
    find_device_index,
    list_devices,
)
from boat_voice.config import load_config


async def main() -> None:
    cfg = load_config()
    print("--- PortAudio sees these devices ---")
    for i, d in enumerate(list_devices()):
        if d.get("max_input_channels", 0) > 0 or d.get("max_output_channels", 0) > 0:
            print(
                f"  [{i}] {d['name']!r:60s} in={d['max_input_channels']} out={d['max_output_channels']} sr={d['default_samplerate']}"
            )

    in_idx = find_device_index(cfg.audio.input_device, "input")
    out_idx = find_device_index(cfg.audio.output_device, "output")
    print(f"\nconfigured input  -> {cfg.audio.input_device}  -> idx={in_idx}")
    print(f"configured output -> {cfg.audio.output_device} -> idx={out_idx}")

    print("\nprobing speaker (open/close)...")
    spk = SpeakerSink(cfg.audio.output_device, cfg.audio.sample_rate_out)
    spk.open()
    print(f"  speaker is_open={spk.is_open}")

    print("\nplaying 1-second 440 Hz tone at 24 kHz (you should hear a beep)...")
    t = np.linspace(0, 1.0, cfg.audio.sample_rate_out, False)
    tone = (0.2 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2").tobytes()
    await spk.play(tone)
    print("  tone done")

    print("\nopening mic for 2 seconds, counting chunks...")
    mic = MicStream(cfg.audio.input_device, cfg.audio.sample_rate_in, cfg.audio.input_chunk_ms)
    mic.start()
    chunks = 0
    total_bytes = 0
    async def _count():
        nonlocal chunks, total_bytes
        async for pcm in mic.chunks():
            chunks += 1
            total_bytes += len(pcm)
    task = asyncio.create_task(_count())
    await asyncio.sleep(2.0)
    mic.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    print(f"  captured {chunks} chunks, {total_bytes} bytes total")
    print(f"  ~ {total_bytes / cfg.audio.sample_rate_in / 2:.2f}s of 16-bit mono PCM")

    print("\nPhase C complete.")


if __name__ == "__main__":
    asyncio.run(main())
