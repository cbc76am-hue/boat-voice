# Contributing to boat-voice (Tolly)

Thanks for your interest. Tolly is a small open-source project — a voice
assistant for a specific boat that happens to be useful as a worked
example of an audio-in / Gemini Live / tools-out architecture. PRs that
broaden the integrations (more SK paths, more HA tool surface, additional
marine services) or improve robustness are welcome.

## Ground rules

- **Tolly is not safety-critical.** She speaks; she does not steer. PRs
  that move toward autopilot integration or that issue commands without a
  human in the loop will not be merged.
- **No secrets in the repo.** API keys, tokens, pairing artifacts all
  belong in `~/.config/boat-voice/` (mode 600), not the codebase. The
  `.gitignore` is conservative; check `git status` before committing.
- **Apache 2.0.** By submitting a PR you agree your contribution is
  Apache-2.0 licensed. No CLA is required.

## Setting up a dev environment

```bash
git clone https://github.com/cbc76am-hue/boat-voice.git
cd boat-voice
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

External services that Tolly talks to (optional for code-only
development; required for live testing):

| Service | Role | Dev-friendly stand-in |
|---|---|---|
| Home Assistant | Sensor reads + entity control | A bare HA install (Docker) with a few sensors |
| Signal K | Waypoint/route store + GPS reads | `signalk-server` from npm, default config |
| marine-router | Chart-aware route planning | Run [marine-router](https://github.com/cbc76am-hue/marine-router) locally on port 8090 |
| OpenCPN | Chart display + live R&M Manager push | OpenCPN 5.10+ with REST RemoteControl paired |
| Gemini Live | The LLM itself | A Google AI Studio API key |

Tolly degrades gracefully if any of router / OpenCPN are offline: voice
tools that need them return a friendly error; everything else still
works.

## Config

Copy `~/.config/boat-voice/config.yaml.example` (if present) or build
yours from scratch — see the schema in `boat_voice/config.py`. Required
fields: `gemini.api_key`, `ha.url`, `ha.long_lived_token`. Optional
fields for the new integrations: `sk.url`, `sk.token_path`, `router.url`,
`opencpn.rest_credentials_path`.

## Running

```bash
# directly
python -m boat_voice

# via systemd (after writing a unit)
systemctl --user start boat-voice

# health
curl http://127.0.0.1:8765/healthz | jq
```

## Style + design conventions

A few habits the codebase tries to follow:

- **No defensive coding against impossible states.** Catch exceptions only
  at real boundaries (filesystem, network, parsing untrusted input). Don't
  wrap internal helpers in `try/except Exception` to "be safe."
- **Comments explain WHY, not WHAT.** If well-named code already says
  what it does, no comment is needed.
- **Async all the way down.** External I/O (HA, SK, router, OpenCPN) is
  async aiohttp; tool dispatch is async; the SK + OpenCPN dual-push uses
  `asyncio.gather` so voice latency isn't doubled.
- **Mirror existing client shapes.** New service clients should look like
  `boat_voice/sk_api.py` — async wrapper over an `aiohttp.ClientSession`,
  `ping()` returning bool, headers built in `__init__`.

## How to submit a PR

1. Fork the repo and create a topic branch off `main`.
2. Make the change. Smoke-test by running `boat-voice` and calling
   `dispatch_tool(...)` from a small ad-hoc script (no voice required) —
   see how Phase 5's smoke test was structured.
3. Open the PR against `main`. In the body, describe what the user-facing
   change is in plain language ("Tolly can now do X"), what services it
   depends on, and any new config keys.
4. Expect review notes on narrowing exception handling, comment hygiene,
   and adherence to the existing module patterns.

## Open work

- **Wake word** ("Hey Tolly"). Currently push-to-talk only.
- **Local LLM fallback.** Whisper + a small local model when internet
  is down at anchor.
- **More HA tool surface.** The current `HassTurnOn`/`HassTurnOff` pair
  is narrow; richer scene/automation/script invocation would help.
- **Multi-output audio.** The current target is one set of speakers;
  flybridge + saloon would be useful.
- **Logbook tool.** Persist key conversations to SK as a daily log entry.

## Code of conduct

Be kind. Be specific. Assume good intent.
