"""Whisper STT wrapper for boat-voice — batch transcribe of one captured utterance.

faster-whisper is synchronous; we wrap it for the asyncio event loop by punting
the heavy calls (model load, transcribe) into the default thread executor. The
boat-voice service is long-lived, so the model is loaded once at startup via
`load()` and the same instance is reused for every turn. This module is not a
streaming STT — it expects a complete PCM blob per call (one-shot turns).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import numpy as np
from faster_whisper import WhisperModel


LOGGER = logging.getLogger(__name__)

# Whisper's VAD + decoder produces nonsense on sub-half-second clips; below this
# many samples at 16 kHz we just return "" rather than burn CPU on garbage.
_MIN_SAMPLES_16K = int(0.2 * 16000)

# Initial prompt fed to Whisper to bias the decoder toward Salish Sea
# proper nouns + marine vocabulary + boat-specific terms.  Observed
# failures without this: "Mukilteo" -> "Muppa Tio", "OpenCPN" -> "open
# CDN", "anchorage" -> "mortgage", "Edmonds" -> "endomens".
_MARINE_INITIAL_PROMPT = (
    "OpenCPN, Signal K, Tolly, Tollycraft, MerCruiser, "
    "Anacortes, Bellingham, Squalicum, Friday Harbor, Roche Harbor, "
    "Eastsound, Deer Harbor, Rosario, West Sound, Olga, "
    "Lopez Village, Hunter Bay, Spencer Spit, Reid Harbor, Stuart Island, "
    "Sucia Island, Matia, Patos, Jones Island, Spieden, Decatur, Cypress, "
    "Coupeville, Penn Cove, Langley, Oak Harbor, Cornet Bay, Deception Pass, "
    "Port Townsend, Point Hudson, Hood Canal, Quilcene, "
    "Edmonds, Mukilteo, Kingston, Shilshole, Bell Harbor, "
    "Bainbridge, Eagle Harbor, Bremerton, Poulsbo, Tacoma, Gig Harbor, "
    "Olympia, Port Angeles, Sequim, Bedwell Harbour, Sidney, "
    "Skagit Bay, Padilla Bay, Swinomish, Shelter Bay, La Conner, "
    "Rosario Strait, Haro Strait, San Juan Channel, Boundary Pass, "
    "Whidbey, San Juan Islands, Puget Sound, Strait of Juan de Fuca, "
    "anchorage, slack, ebb, flood, knots, fathoms, draft, beam, "
    "PlanRoute, waypoint, route, GPS, depth, AIS, NMEA, VHF, "
    "starboard, port, helm, transom, fender, rode, windlass"
)


class WhisperSTT:
    """One-shot Whisper transcription. Load once, transcribe many."""

    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model: Optional[WhisperModel] = None
        self._load_lock = asyncio.Lock()

    @property
    def language(self) -> str:
        return self._language

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model_size(self) -> str:
        return self._model_size

    async def load(self) -> None:
        """Load the model into memory. Idempotent; safe to call concurrently."""
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            loop = asyncio.get_running_loop()

            def _load() -> WhisperModel:
                return WhisperModel(
                    self._model_size,
                    device=self._device,
                    compute_type=self._compute_type,
                )

            try:
                self._model = await loop.run_in_executor(None, _load)
            except Exception as err:
                raise RuntimeError(
                    f"Failed to load Whisper model {self._model_size!r}: {err}"
                ) from err
            LOGGER.info(
                "Whisper model loaded: %s (device=%s, compute=%s)",
                self._model_size, self._device, self._compute_type,
            )

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        """Transcribe a 16-bit mono PCM blob. Returns stripped text, possibly empty."""
        if self._model is None:
            raise RuntimeError("WhisperSTT.load() must be called first")
        # Boat mic is fixed at 16 kHz; refusing other rates avoids silent quality
        # loss from an ad-hoc resample path and forces upstream to be explicit.
        if sample_rate != 16000:
            raise ValueError(
                f"WhisperSTT expects 16 kHz PCM, got {sample_rate} Hz "
                "(boat mic should be 16 kHz mono s16le)"
            )
        if not pcm_bytes:
            return ""

        audio = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size < _MIN_SAMPLES_16K:
            LOGGER.debug("transcribe: clip too short (%d samples), skipping", audio.size)
            return ""

        loop = asyncio.get_running_loop()
        model = self._model

        def _run() -> str:
            segments, _info = model.transcribe(
                audio,
                language=self._language,
                beam_size=5,           # was 1; bigger beam = better accuracy on
                                       # ambiguous audio at modest extra CPU
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=_MARINE_INITIAL_PROMPT,
            )
            return "".join(seg.text for seg in segments).strip()

        try:
            return await loop.run_in_executor(None, _run)
        except Exception as err:
            raise RuntimeError(f"Whisper transcribe failed: {err}") from err

    async def close(self) -> None:
        """Drop the model reference so GC can reclaim it. Idempotent."""
        if self._model is None:
            return
        self._model = None
        LOGGER.debug("Whisper model released")
