"""Phase B smoke test: open a Gemini Live session, send text, watch tools fire."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from boat_voice.config import load_config
from boat_voice.gemini import GeminiLiveSession
from boat_voice.prompts import build_system_prompt
from boat_voice.tools import get_tool_declarations

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CHEATSHEET = "Saloon Temp=sensor.signalk_environment_inside_temperature, Depth=sensor.signalk_environment_depth_below_keel"


async def dispatch(name: str, args: dict) -> dict:
    print(f"[tool] {name}({args})")
    if name == "GetLiveContext":
        return {"context": "Saloon Temp: 72.5 F\nDepth: 14.2 ft\n"}
    if name == "GetDateTime":
        return {"datetime": datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")}
    if name == "DiagnoseSelf":
        return {"report": "test stub: everything fake-OK."}
    return {"result": "stub ok"}


async def main() -> None:
    cfg = load_config()
    session = GeminiLiveSession(
        api_key=cfg.gemini.api_key,
        model=cfg.gemini.model,
        voice=cfg.gemini.voice,
        system_prompt=build_system_prompt(CHEATSHEET, cfg.home_port.name),
        tool_declarations=get_tool_declarations(CHEATSHEET),
        tool_dispatcher=dispatch,
        input_sample_rate=cfg.audio.sample_rate_in,
        silence_end_ms=cfg.conversation.silence_end_ms,
        start_sensitivity=cfg.conversation.start_sensitivity,
        end_sensitivity=cfg.conversation.end_sensitivity,
    )
    await session.connect()
    print("connected ok")
    receive_task = asyncio.create_task(session.receive_loop())
    await session.send_text("Hello Tolly. What's today's date?")
    done = await session.wait_for_turn_complete(timeout=30)
    receive_task.cancel()
    try:
        await receive_task
    except (asyncio.CancelledError, Exception):
        pass

    audio = session.get_buffered_audio()
    print("turn_complete:", done)
    print("audio bytes:", len(audio))
    print("user heard:", session.last_input_transcription)
    print("tolly said:", session.last_output_transcription)
    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
