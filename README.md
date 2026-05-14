# boat-voice — Tolly

Standalone Python service giving the boat a voice assistant called **Tolly**.
Press a button in Home Assistant → speak into the laptop mic → Gemini Live
responds through the laptop speakers.

See `~/BOAT_VOICE_PLAN.md` for the full architecture, and `~/NOTES.md`
("Tolly — Boat Voice Assistant" section) for operator-level docs.

## Quick reference

```bash
# is it running?
systemctl --user status boat-voice

# live logs
journalctl --user -u boat-voice -f

# restart after editing ~/.config/boat-voice/config.yaml
systemctl --user restart boat-voice

# what's the health state?
curl http://127.0.0.1:8765/healthz | jq

# fire a one-shot turn
curl -X POST http://127.0.0.1:8765/talk

# what audio devices does PortAudio see?
./venv/bin/python -m boat_voice.devices_dump
```

## Layout

```
boat-voice/
├── pyproject.toml          # deps: google-genai, sounddevice, aiohttp, pyyaml, numpy
├── venv/                   # virtualenv (gitignored)
└── boat_voice/
    ├── __main__.py         # entry: python -m boat_voice
    ├── main.py             # orchestrator (lifecycle, talk loop, healthz)
    ├── server.py           # aiohttp /talk /conversation_mode /healthz
    ├── gemini.py           # GeminiLiveSession (WSS, tools, session_resumption)
    ├── audio.py            # sounddevice mic + speaker
    ├── tools.py            # tool declarations + dispatch
    ├── ha_api.py           # async HA REST client
    ├── prompts.py          # Tolly system prompt
    ├── config.py           # YAML loader
    ├── noaa.py             # v1.1 stub
    └── devices_dump.py     # debug helper
```

## Config

`~/.config/boat-voice/config.yaml` (mode 600, owner `boat`). Contains the
Gemini API key and HA long-lived token inline. Don't commit it.

## v1 caveats

- No NOAA tides/weather direct API yet — Google Search grounding covers it.
- No wake word ("Hey Tolly") — push-to-talk via HA button only.
- No local-LLM fallback — internet down = Tolly down.
- One audio output target (the laptop). Multi-room is v1.1+.
