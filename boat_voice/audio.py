"""sounddevice-based mic capture + speaker playback for boat-voice.

Gemini Live wants 16 kHz s16le mono in and emits 24 kHz s16le mono out.
PipeWire's ALSA shim normally negotiates a native rate (often 48 kHz); we ask
sounddevice for our target rate and rely on PortAudio's resampler.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import numpy as np
import sounddevice as sd


LOGGER = logging.getLogger(__name__)


def list_devices() -> list[dict]:
    """Return PortAudio's view of devices — useful for diagnostics."""
    return list(sd.query_devices())


def find_device_index(name: str, kind: str) -> int | None:
    """kind='input' or 'output'. Match by case-insensitive substring."""
    needle = name.lower()
    for i, dev in enumerate(sd.query_devices()):
        if kind == "input" and dev.get("max_input_channels", 0) <= 0:
            continue
        if kind == "output" and dev.get("max_output_channels", 0) <= 0:
            continue
        if needle in str(dev.get("name", "")).lower():
            return i
    return None


class MicStream:
    """Async mic capture: emits 16kHz mono s16le PCM chunks of ~input_chunk_ms."""

    def __init__(
        self,
        device: str | None,
        sample_rate: int = 16000,
        chunk_ms: int = 100,
    ) -> None:
        self._device = device
        self._sample_rate = sample_rate
        self._chunk_ms = chunk_ms
        self._frames_per_chunk = int(sample_rate * chunk_ms / 1000)
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self._stream: sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._muted = False
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def _enqueue(self, pcm: bytes) -> None:
        """Push a chunk into the queue, dropping the oldest if it's full.

        Runs on the event loop thread (scheduled via call_soon_threadsafe).
        """
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(pcm)
            except asyncio.QueueFull:
                pass

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            LOGGER.debug("Mic status: %s", status)
        if self._muted:
            return
        # indata is float32 NDArray shape (frames, channels). Take channel 0,
        # convert to int16 PCM bytes.
        mono = indata[:, 0] if indata.ndim > 1 else indata
        pcm = np.clip(mono * 32767.0, -32768, 32767).astype("<i2").tobytes()
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._enqueue, pcm)

    def drain(self) -> None:
        """Drop all currently-buffered chunks. Call before starting a new turn."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        device_index = (
            find_device_index(self._device, "input") if self._device else None
        )
        if self._device and device_index is None:
            LOGGER.warning(
                "Configured mic device %r not found by PortAudio — "
                "falling back to PortAudio default. "
                "Run `python -m boat_voice.devices_dump` to see valid names.",
                self._device,
            )
        self._stream = sd.InputStream(
            device=device_index,
            samplerate=self._sample_rate,
            blocksize=self._frames_per_chunk,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        self._open = True
        LOGGER.info(
            "Mic open: device=%s sr=%d frames/chunk=%d",
            self._device, self._sample_rate, self._frames_per_chunk,
        )

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._open = False

    async def chunks(self) -> AsyncIterator[bytes]:
        """Async iterator over mic chunks; stops when stop() is called."""
        while self._open:
            try:
                pcm = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                yield pcm
            except asyncio.TimeoutError:
                continue


class StreamingPlayback:
    """A live PortAudio output stream that plays s16le mono PCM as it arrives.

    Created per Gemini turn. Receive-loop pushes chunks via `feed_nowait`; a
    dedicated consumer task writes them to PortAudio. `finish()` flushes and
    closes the stream — it returns only after PortAudio has actually drained
    its internal buffer.
    """

    def __init__(self, device_index: int | None, sample_rate: int) -> None:
        self._stream = sd.RawOutputStream(
            device=device_index,
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        )
        self._stream.start()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._consumer_task = asyncio.create_task(
            self._consume(), name="streaming_playback_consume"
        )
        self._closed = False

    async def _consume(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                break
            try:
                await loop.run_in_executor(None, self._stream.write, chunk)
            except Exception as err:
                LOGGER.warning("playback write failed: %s", err)

    def feed_nowait(self, pcm: bytes) -> None:
        """Non-blocking enqueue. Safe to call from a sync callback on the loop."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            LOGGER.warning("playback queue full — dropping chunk")

    async def finish(self) -> None:
        """Signal end-of-turn, await drain, then close the stream."""
        if self._closed:
            return
        self._closed = True
        # Sentinel telling consumer to exit once all queued chunks have been written.
        await self._queue.put(None)
        try:
            await self._consumer_task
        except Exception as err:
            LOGGER.warning("playback consumer crashed: %s", err)
        loop = asyncio.get_running_loop()
        # Pa_StopStream waits for PortAudio's internal buffer to play out
        # before returning, so this is the actual end-of-audio point.
        await loop.run_in_executor(None, self._stream.stop)
        try:
            self._stream.close()
        except Exception:
            pass


class SpeakerSink:
    """Owns the speaker device. open() probes; start_stream() opens a per-turn streaming sink."""

    def __init__(self, device: str | None, sample_rate: int = 24000) -> None:
        self._device = device
        self._sample_rate = sample_rate
        self._open = False
        self._device_index: int | None = None

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._device_index = (
            find_device_index(self._device, "output") if self._device else None
        )
        if self._device and self._device_index is None:
            LOGGER.warning(
                "Configured speaker device %r not found by PortAudio — "
                "falling back to PortAudio default.",
                self._device,
            )
        try:
            stream = sd.OutputStream(
                device=self._device_index,
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
            )
            stream.start()
            stream.stop()
            stream.close()
            self._open = True
            LOGGER.info(
                "Speaker probe ok: device=%s sr=%d",
                self._device, self._sample_rate,
            )
        except Exception as err:
            self._open = False
            LOGGER.error("Speaker probe failed: %s", err)
            raise

    def close(self) -> None:
        self._open = False

    def start_stream(self) -> StreamingPlayback:
        """Open a streaming sink for one Gemini turn. Caller must `await .finish()`."""
        return StreamingPlayback(self._device_index, self._sample_rate)

    async def play(self, pcm_bytes: bytes) -> None:
        """Blob playback (kept for the Phase B / phase-C test scripts)."""
        if not pcm_bytes:
            return
        data = np.frombuffer(pcm_bytes, dtype="<i2").astype("float32") / 32768.0

        def _blocking_play() -> None:
            sd.play(
                data,
                samplerate=self._sample_rate,
                device=self._device_index,
                blocking=True,
            )

        await asyncio.get_running_loop().run_in_executor(None, _blocking_play)
