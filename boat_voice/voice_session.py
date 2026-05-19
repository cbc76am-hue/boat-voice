"""Voice session orchestrator: mic -> VAD -> STT -> Claude -> TTS -> speaker.

Replaces the Gemini Live session.  One turn per /talk POST: capture a single
utterance, transcribe with Whisper, send through Claude (which may invoke
tools), synthesize the response with Piper, play it out.

Conversation history lives on the Claude wrapper; this class is stateless
between turns aside from holding loaded STT/TTS/LLM handles.
"""
from __future__ import annotations

import asyncio
import audioop  # stdlib; deprecated in 3.13 but boat runs 3.12
import logging
import re
from typing import Any, Awaitable, Callable

import webrtcvad


# Markdown-to-plain-text sanitizer for TTS.  Claude is prompted not to emit
# markdown, but belt-and-suspenders: any asterisks/backticks/headers that
# leak through get stripped before Piper synthesizes them as literal
# "asterisk asterisk Roche Harbor asterisk asterisk".
_MD_PATTERNS = [
    (re.compile(r"```[\s\S]*?```"), ""),          # code fences -> drop body
    (re.compile(r"`([^`]+)`"), r"\1"),             # `inline code` -> inline code
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),  # **bold**
    (re.compile(r"__(.+?)__", re.DOTALL), r"\1"),  # __bold__
    (re.compile(r"\*([^\s*].*?[^\s*]|\S)\*", re.DOTALL), r"\1"),  # *italic*
    (re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)"), r"\1"),  # _italic_
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),  # [text](url) -> text
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),  # # heading
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),  # bullet markers
]


def _strip_markdown(text: str) -> str:
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)
    # Collapse any residual runs of whitespace inside text (markdown stripping
    # can leave double-spaces where bullets were).
    return re.sub(r"[ \t]+", " ", text).strip()


# Slow tools get a spoken "working on it" ack before they run, so the
# operator hears feedback instead of 30 seconds of silence while the router
# crunches a route.  Phrased to take ~2 seconds of speech.
_SLOW_TOOL_ACKS = {
    "PlanRoute": "Hold on, working on that route.",
}

from .audio import MicStream, SpeakerSink
from .claude_llm import ClaudeLLM
from .stt import WhisperSTT
from .tts import PiperTTS


LOGGER = logging.getLogger(__name__)


ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
StateCallback = Callable[[str], Awaitable[None]]


class VoiceSession:
    """One turn at a time: capture, transcribe, think, speak."""

    def __init__(
        self,
        *,
        stt: WhisperSTT,
        tts: PiperTTS,
        llm: ClaudeLLM,
        mic: MicStream,
        speaker: SpeakerSink,
        mic_sample_rate: int = 16000,
        speaker_sample_rate: int = 24000,
        vad_aggressiveness: int = 2,
        max_capture_s: float = 15.0,
        post_speech_silence_s: float = 1.2,
        pre_speech_silence_s: float = 3.0,
        min_speech_s: float = 0.3,
    ) -> None:
        self._stt = stt
        self._tts = tts
        self._llm = llm
        self._mic = mic
        self._speaker = speaker
        self._mic_sr = mic_sample_rate
        self._speaker_sr = speaker_sample_rate
        self._vad = webrtcvad.Vad(vad_aggressiveness)
        self._max_capture_s = max_capture_s
        self._post_silence_s = post_speech_silence_s
        self._pre_silence_s = pre_speech_silence_s
        self._min_speech_s = min_speech_s
        # webrtcvad expects 10/20/30ms frames at 8/16/32/48 kHz.
        if mic_sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError(
                f"webrtcvad requires 8/16/32/48 kHz; got {mic_sample_rate}"
            )
        self._frame_ms = 30
        self._frame_bytes = int(mic_sample_rate * self._frame_ms / 1000) * 2  # int16

    async def start(self) -> None:
        """Load Whisper + Piper models.  Idempotent."""
        await self._stt.load()
        await self._tts.load()
        LOGGER.info(
            "voice_session ready (stt=%s, tts_rate=%dHz, llm_history=%d)",
            self._stt.model_size if hasattr(self._stt, "model_size") else "?",
            self._tts.sample_rate,
            self._llm.history_length,
        )

    async def close(self) -> None:
        await self._stt.close()
        await self._tts.close()

    async def turn(
        self,
        dispatcher: ToolDispatcher,
        on_state: StateCallback | None = None,
    ) -> str:
        """Run one full turn.  Returns the user's transcribed text (for
        logging) or empty string if nothing was captured."""
        if on_state:
            await on_state("listening")
        pcm = await self._capture()
        if not pcm:
            LOGGER.info("VoiceSession: no speech captured")
            return ""

        if on_state:
            await on_state("thinking")
        try:
            user_text = await self._stt.transcribe(pcm, sample_rate=self._mic_sr)
        except Exception as err:
            LOGGER.exception("STT failed: %s", err)
            await self._speak_error("Sorry, I couldn't make out what you said.")
            return ""
        user_text = (user_text or "").strip()
        if not user_text:
            LOGGER.info("VoiceSession: STT empty")
            return ""
        LOGGER.info("STT: %s", user_text)

        try:
            response_text = await self._llm.turn(
                user_text, dispatcher,
                on_tool_start=self._on_tool_start,
            )
        except Exception as err:
            LOGGER.exception("Claude turn failed: %s", err)
            await self._speak_error("Sorry, I lost the assistant. Try again.")
            return user_text

        response_text = (response_text or "").strip()
        if not response_text:
            LOGGER.info("VoiceSession: LLM returned empty")
            await self._speak_error("I'm not sure how to answer that.")
            return user_text

        LOGGER.info("LLM: %s", response_text[:200])
        if on_state:
            await on_state("speaking")
        await self._synth_and_play(response_text)
        return user_text

    async def _capture(self) -> bytes:
        """VAD-driven mic capture.  Returns int16 mono PCM at mic_sample_rate."""
        self._mic.drain()

        captured = bytearray()
        carry = bytearray()

        max_frames = int(self._max_capture_s * 1000 / self._frame_ms)
        pre_silence_max = int(self._pre_silence_s * 1000 / self._frame_ms)
        post_silence_max = int(self._post_silence_s * 1000 / self._frame_ms)
        min_speech_frames = max(1, int(self._min_speech_s * 1000 / self._frame_ms))

        speech_frames = 0
        silence_after_speech = 0
        silence_before_speech = 0
        total_frames = 0
        had_speech = False

        async for chunk in self._mic.chunks():
            carry.extend(chunk)
            while len(carry) >= self._frame_bytes:
                frame = bytes(carry[:self._frame_bytes])
                del carry[:self._frame_bytes]

                is_speech = self._vad.is_speech(frame, self._mic_sr)
                captured.extend(frame)
                total_frames += 1

                if is_speech:
                    speech_frames += 1
                    silence_after_speech = 0
                    had_speech = True
                else:
                    if had_speech:
                        silence_after_speech += 1
                    else:
                        silence_before_speech += 1

                # End conditions, in priority order:
                if total_frames >= max_frames:
                    LOGGER.debug("capture: hit max duration")
                    return bytes(captured) if had_speech else b""
                if had_speech and speech_frames >= min_speech_frames \
                        and silence_after_speech >= post_silence_max:
                    LOGGER.debug(
                        "capture: end-of-speech (%d frames speech + %d silence)",
                        speech_frames, silence_after_speech,
                    )
                    return bytes(captured)
                if not had_speech and silence_before_speech >= pre_silence_max:
                    LOGGER.debug("capture: no speech detected in pre-window")
                    return b""

        return bytes(captured) if had_speech else b""

    async def _on_tool_start(self, name: str, args: dict[str, Any]) -> None:
        """Speak a brief ack for slow tools so the operator hears feedback
        before the actual work completes.  Fast tools (no ack mapped) are
        no-ops."""
        ack = _SLOW_TOOL_ACKS.get(name)
        if not ack:
            return
        LOGGER.info("slow tool start: %s — speaking ack", name)
        try:
            pcm = await self._tts.synthesize(ack)
        except Exception as err:
            LOGGER.warning("tool-start ack synth failed: %s", err)
            return
        if pcm:
            await self._play_pcm(pcm, src_rate=self._tts.sample_rate)

    async def _synth_and_play(self, text: str) -> None:
        spoken = _strip_markdown(text)
        if spoken != text:
            LOGGER.debug("TTS: stripped markdown (%d -> %d chars)",
                         len(text), len(spoken))
        try:
            pcm = await self._tts.synthesize(spoken)
        except Exception as err:
            LOGGER.exception("TTS failed: %s", err)
            return
        if not pcm:
            return
        await self._play_pcm(pcm, src_rate=self._tts.sample_rate)

    async def _speak_error(self, msg: str) -> None:
        """Best-effort error speech.  Used when the main pipeline fails."""
        try:
            pcm = await self._tts.synthesize(msg)
            if pcm:
                await self._play_pcm(pcm, src_rate=self._tts.sample_rate)
        except Exception as err:
            LOGGER.warning("error-speak failed: %s", err)

    async def _play_pcm(self, pcm: bytes, src_rate: int) -> None:
        if not self._speaker.is_open:
            LOGGER.warning("speaker not open; dropping %d bytes", len(pcm))
            return
        if src_rate != self._speaker_sr:
            pcm, _ = audioop.ratecv(
                pcm, 2, 1, src_rate, self._speaker_sr, None,
            )
        # Mute mic during TTS playback so Tolly doesn't hear her own voice
        # and feed it into the next capture / conv-mode phrase-sniff.
        self._mic.set_muted(True)
        try:
            playback = self._speaker.start_stream()
            playback.feed_nowait(pcm)
            await playback.finish()
        finally:
            # Drain any residual mic chunks that snuck through before mute
            # took effect (the sounddevice callback runs on its own thread).
            self._mic.drain()
            self._mic.set_muted(False)
