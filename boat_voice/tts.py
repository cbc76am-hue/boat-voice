"""Piper TTS wrapper for boat-voice — batch synth of one utterance at a time.

piper-tts is synchronous; we wrap it for the asyncio event loop by punting the
heavy calls (model load, synthesize) into the default thread executor. The
boat-voice service is long-lived, so the voice model is loaded once at startup
via `load()` and reused for every turn. Output is raw int16 mono PCM at the
voice's native rate (typically 22050 Hz for medium-quality Piper voices); the
caller is responsible for any resample needed to match the speaker rate
(SpeakerSink defaults to 24000 Hz — see boat_voice/audio.py).

Voice files (.onnx + .onnx.json) are not bundled with piper-tts. We probe a
few candidate locations and, if none are populated, download `en_US-amy-medium`
to ~/.config/boat-voice/piper-voice/ at startup. Download failure raises at
load() so the service fails fast rather than surprising us mid-conversation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.request
from pathlib import Path
from typing import Optional

from piper import PiperVoice


LOGGER = logging.getLogger(__name__)

_DEFAULT_VOICE_NAME = "en_US-amy-medium"
_DEFAULT_VOICE_BASE_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
    "en/en_US/amy/medium"
)
_DEFAULT_DOWNLOAD_DIR = Path.home() / ".config" / "boat-voice" / "piper-voice"

# Probe order: env var wins; then the most "intended for piper" paths first.
_CANDIDATE_DIRS: tuple[Path, ...] = (
    Path.home() / ".local" / "share" / "piper-voices",
    Path.home() / "piper-voices",
    _DEFAULT_DOWNLOAD_DIR,
    Path("/usr/share/piper-voices"),
)


def _discover_voice() -> Optional[Path]:
    env_path = os.environ.get("PIPER_VOICE_PATH")
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            return p
        LOGGER.warning("PIPER_VOICE_PATH=%s does not point at a file", env_path)
    for d in _CANDIDATE_DIRS:
        if not d.is_dir():
            continue
        matches = sorted(d.glob("*.onnx"))
        if matches:
            return matches[0]
    return None


def _download_default_voice() -> Path:
    _DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    model_path = _DEFAULT_DOWNLOAD_DIR / f"{_DEFAULT_VOICE_NAME}.onnx"
    config_path = _DEFAULT_DOWNLOAD_DIR / f"{_DEFAULT_VOICE_NAME}.onnx.json"
    targets = [
        (f"{_DEFAULT_VOICE_BASE_URL}/{_DEFAULT_VOICE_NAME}.onnx", model_path),
        (f"{_DEFAULT_VOICE_BASE_URL}/{_DEFAULT_VOICE_NAME}.onnx.json", config_path),
    ]
    for url, dst in targets:
        if dst.is_file() and dst.stat().st_size > 0:
            continue
        LOGGER.info("Downloading Piper voice asset: %s -> %s", url, dst)
        tmp = dst.with_suffix(dst.suffix + ".part")
        try:
            urllib.request.urlretrieve(url, tmp)
            tmp.replace(dst)
        except Exception as err:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise RuntimeError(
                f"Failed to download Piper voice asset from {url}: {err}"
            ) from err
    return model_path


class PiperTTS:
    """One-shot Piper synthesis. Load once, synthesize many."""

    def __init__(self, voice_path: str | None = None) -> None:
        self._voice_path: Optional[Path] = Path(voice_path) if voice_path else None
        self._voice: Optional[PiperVoice] = None
        self._sample_rate: Optional[int] = None
        self._load_lock = asyncio.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._voice is not None

    @property
    def sample_rate(self) -> int:
        if self._sample_rate is None:
            raise RuntimeError("PiperTTS.load() must be called first")
        return self._sample_rate

    async def load(self) -> None:
        """Discover/download voice if needed, then load the model. Idempotent."""
        if self._voice is not None:
            return
        async with self._load_lock:
            if self._voice is not None:
                return
            loop = asyncio.get_running_loop()
            if self._voice_path is None:
                discovered = _discover_voice()
                if discovered is None:
                    LOGGER.info(
                        "No Piper voice found in probed paths; downloading default %s",
                        _DEFAULT_VOICE_NAME,
                    )
                    self._voice_path = await loop.run_in_executor(
                        None, _download_default_voice
                    )
                else:
                    self._voice_path = discovered
            if not self._voice_path.is_file():
                raise RuntimeError(
                    f"Piper voice file not found: {self._voice_path}"
                )

            voice_path = self._voice_path

            def _load() -> PiperVoice:
                return PiperVoice.load(str(voice_path))

            try:
                voice = await loop.run_in_executor(None, _load)
            except Exception as err:
                raise RuntimeError(
                    f"Failed to load Piper voice {voice_path}: {err}"
                ) from err
            self._voice = voice
            self._sample_rate = int(voice.config.sample_rate)
            LOGGER.info(
                "Piper voice loaded: %s (rate=%d)",
                voice_path, self._sample_rate,
            )

    async def synthesize(self, text: str) -> bytes:
        """Synthesize `text` to raw int16 mono PCM at `self.sample_rate`."""
        if self._voice is None:
            raise RuntimeError("PiperTTS.load() must be called first")
        if not text or not text.strip():
            return b""

        voice = self._voice
        loop = asyncio.get_running_loop()

        def _run() -> bytes:
            parts: list[bytes] = []
            for chunk in voice.synthesize(text):
                parts.append(chunk.audio_int16_bytes)
            return b"".join(parts)

        try:
            return await loop.run_in_executor(None, _run)
        except Exception as err:
            raise RuntimeError(f"Piper synthesize failed: {err}") from err

    async def close(self) -> None:
        """Drop the voice reference so GC can reclaim it. Idempotent."""
        if self._voice is None:
            return
        self._voice = None
        self._sample_rate = None
        LOGGER.debug("Piper voice released")
