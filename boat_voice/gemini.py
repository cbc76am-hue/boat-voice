"""Gemini Live WebSocket session manager — boat-voice variant.

Adapted from the Sierra ESPHome integration's gemini_session.py. The main
deviation: this version is HA-agnostic at the session level (the session calls
back into a tool dispatcher passed in by the host), so the same class works
without depending on `hass`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from google import genai
from google.genai import types


LOGGER = logging.getLogger(__name__)


ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
ConversationModeListener = Callable[[bool], Awaitable[None]]
AudioChunkHandler = Callable[[bytes], None]


class GeminiLiveSession:
    """Manages a single Gemini Live API WebSocket session."""

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        system_prompt: str,
        tool_declarations: list[dict[str, Any]],
        tool_dispatcher: ToolDispatcher,
        input_sample_rate: int = 16000,
        silence_end_ms: int = 1500,
        start_sensitivity: str = "START_SENSITIVITY_HIGH",
        end_sensitivity: str = "END_SENSITIVITY_LOW",
        thinking_level: str = "minimal",
        on_conversation_mode_change: ConversationModeListener | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._system_prompt = system_prompt
        self._tool_declarations = tool_declarations
        self._tool_dispatcher = tool_dispatcher
        self._input_sample_rate = input_sample_rate
        self._silence_end_ms = silence_end_ms
        self._start_sensitivity = start_sensitivity
        self._end_sensitivity = end_sensitivity
        self._thinking_level = thinking_level
        self._on_conversation_mode_change = on_conversation_mode_change
        self._audio_chunk_handler: AudioChunkHandler | None = None

        self._client: Any = None
        self._session: Any = None
        self._session_cm: Any = None
        self._connected = False
        self._session_handle: str | None = None
        self._turn_complete_event = asyncio.Event()
        self._response_audio_buffer = bytearray()
        self._last_input_transcription: str | None = None
        self._last_output_transcription: str | None = None
        self._responding = False
        self._audio_chunks_sent = 0
        self._conversation_mode = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def responding(self) -> bool:
        return self._responding

    @property
    def conversation_mode(self) -> bool:
        return self._conversation_mode

    def set_conversation_mode(self, active: bool) -> None:
        self._conversation_mode = bool(active)

    @property
    def last_input_transcription(self) -> str | None:
        return self._last_input_transcription

    @property
    def last_output_transcription(self) -> str | None:
        return self._last_output_transcription

    def get_buffered_audio(self) -> bytes:
        data = bytes(self._response_audio_buffer)
        self._response_audio_buffer.clear()
        return data

    def clear_turn_complete(self) -> None:
        self._turn_complete_event.clear()
        self._audio_chunks_sent = 0
        self._responding = False

    def set_audio_chunk_handler(self, handler: AudioChunkHandler | None) -> None:
        """Install a sync callback that gets each 24kHz PCM chunk as it arrives.

        When set, the session no longer buffers audio internally — the host is
        responsible for playback. When None, audio is buffered and available via
        get_buffered_audio() at turn_complete time (legacy path used by tests).
        """
        self._audio_chunk_handler = handler

    async def connect(self) -> None:
        """Open the Gemini Live WebSocket session."""
        if self._client is None:
            # Cert/keyring init happens off-loop in an executor (Sierra pattern).
            loop = asyncio.get_running_loop()
            self._client = await loop.run_in_executor(
                None, lambda: genai.Client(api_key=self._api_key)
            )

        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._voice
                    )
                )
            ),
            system_instruction=types.Content(
                parts=[types.Part(text=self._system_prompt)]
            ),
            thinking_config=types.ThinkingConfig(
                thinking_level=self._thinking_level
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=self._start_sensitivity,
                    end_of_speech_sensitivity=self._end_sensitivity,
                    silence_duration_ms=self._silence_end_ms,
                ),
                activity_handling="START_OF_ACTIVITY_INTERRUPTS",
            ),
            tools=[
                types.Tool(function_declarations=[
                    types.FunctionDeclaration(**td) for td in self._tool_declarations
                ]),
                types.Tool(google_search=types.GoogleSearch()),
            ],
        )

        if self._session_handle:
            config.session_resumption = types.SessionResumptionConfig(
                handle=self._session_handle,
            )

        self._session_cm = self._client.aio.live.connect(
            model=self._model, config=config
        )
        self._session = await self._session_cm.__aenter__()
        self._connected = True
        LOGGER.info("Gemini Live session opened (model=%s)", self._model)

    async def send_text(self, text: str) -> None:
        """Send a text turn (used for tests + DiagnoseSelf triggers)."""
        if not self._connected or not self._session:
            return
        await self._session.send_client_content(
            turns=[types.Content(role="user", parts=[types.Part(text=text)])],
            turn_complete=True,
        )

    async def send_audio(self, chunk: bytes) -> None:
        """Send a raw 16kHz PCM audio chunk to Gemini."""
        if not self._connected or not self._session or self._responding:
            return
        self._audio_chunks_sent += 1
        if self._audio_chunks_sent <= 3 or self._audio_chunks_sent % 50 == 0:
            LOGGER.debug(
                "Mic chunk #%d -> Gemini (%d bytes)",
                self._audio_chunks_sent,
                len(chunk),
            )
        await self._session.send_realtime_input(
            audio=types.Blob(
                data=chunk,
                mime_type=f"audio/pcm;rate={self._input_sample_rate}",
            )
        )

    async def receive_loop(self) -> None:
        """Drain incoming messages until turn completes or session interrupts."""
        if not self._session:
            return

        self._turn_complete_event.clear()
        self._response_audio_buffer.clear()
        self._last_input_transcription = None
        self._last_output_transcription = None
        self._responding = False

        try:
            async for response in self._session.receive():
                if response.session_resumption_update:
                    update = response.session_resumption_update
                    if update.resumable and update.new_handle:
                        self._session_handle = update.new_handle

                if response.go_away:
                    LOGGER.info(
                        "Gemini goAway received, time_left=%s",
                        response.go_away.time_left,
                    )
                    break

                if response.tool_call:
                    await self._handle_tool_calls(response.tool_call)
                    continue

                if response.tool_call_cancellation:
                    LOGGER.info(
                        "Tool calls cancelled: %s",
                        response.tool_call_cancellation.ids,
                    )
                    continue

                if not response.server_content:
                    continue

                content = response.server_content

                if content.input_transcription and content.input_transcription.text:
                    text = content.input_transcription.text.strip()
                    if text:
                        self._last_input_transcription = text
                        LOGGER.debug("STT: %s", text)

                if content.output_transcription and content.output_transcription.text:
                    text = content.output_transcription.text.strip()
                    if text:
                        if self._last_output_transcription:
                            self._last_output_transcription += text
                        else:
                            self._last_output_transcription = text
                        LOGGER.debug("TTS: %s", text)

                if content.model_turn and content.model_turn.parts:
                    for part in content.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            if not self._responding:
                                self._responding = True
                                LOGGER.debug("Gemini responding")
                            data = part.inline_data.data
                            if self._audio_chunk_handler is not None:
                                try:
                                    self._audio_chunk_handler(data)
                                except Exception as err:
                                    LOGGER.warning(
                                        "audio_chunk_handler raised: %s", err
                                    )
                            else:
                                self._response_audio_buffer.extend(data)

                if content.turn_complete:
                    LOGGER.info(
                        "Gemini turn complete: %d bytes of audio buffered",
                        len(self._response_audio_buffer),
                    )
                    self._turn_complete_event.set()
                    break

                if content.interrupted:
                    LOGGER.info(
                        "Gemini interrupted, keeping %d bytes",
                        len(self._response_audio_buffer),
                    )
                    self._turn_complete_event.set()
                    break

        except Exception as err:
            LOGGER.error("Gemini receive error: %s", err)
            self._turn_complete_event.set()
            raise

    async def _handle_tool_calls(self, tool_call: Any) -> None:
        """Dispatch tool calls via the host's tool dispatcher and send responses back."""
        function_responses = []

        for fc in tool_call.function_calls:
            LOGGER.info("Tool call: %s(%s)", fc.name, fc.args)

            if fc.name == "set_conversation_mode":
                active = bool((fc.args or {}).get("active", False))
                self.set_conversation_mode(active)
                if self._on_conversation_mode_change is not None:
                    try:
                        await self._on_conversation_mode_change(active)
                    except Exception as err:
                        LOGGER.warning("conversation-mode listener failed: %s", err)
                result = {
                    "result": f"Conversation mode {'enabled' if active else 'disabled'}"
                }
            else:
                try:
                    result = await self._tool_dispatcher(fc.name, fc.args or {})
                except Exception as err:
                    LOGGER.exception("Dispatcher failed for %s", fc.name)
                    result = {"error": f"Failed to execute {fc.name}: {err}"}

            function_responses.append(
                types.FunctionResponse(name=fc.name, id=fc.id, response=result)
            )

        await self._session.send_tool_response(function_responses=function_responses)

    async def wait_for_turn_complete(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._turn_complete_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            LOGGER.warning("Gemini turn timed out after %.1fs", timeout)
            return False

    async def close(self) -> None:
        self._connected = False
        if self._session_cm:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
            self._session_cm = None
        LOGGER.info("Gemini Live session closed")
