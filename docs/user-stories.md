# Voxtera User Stories

This file is the canonical home for all user stories. New stories are added by the architect; the dev team breaks each into Jira tickets under the matching epic.

---

## VOX-6 — Local Voice Loop (under epic VOX-E1)

### Story

> As a developer building Voxtera, I want a working local voice agent on my laptop that listens to me speak in any language, understands me, and replies with spoken audio in the same language, so that I can validate the core conversation pipeline (STT → LLM → TTS) and the multilingual logic before investing in WebRTC, hosting, or production TTS.

### Acceptance criteria

**Given** the bot is running locally on my laptop
**When** I speak into my microphone in English
**Then** Whisper transcribes my speech, Claude responds as a tourism assistant, and I hear the reply in English through my speakers within ~3 seconds.

**Given** the bot is running
**When** I speak into my microphone in French (or Spanish, Italian, Japanese, etc.)
**Then** the bot detects the language automatically, Claude replies in that same language, and the TTS speaks the reply in that language.

**Given** I am mid-conversation in one language
**When** I continue speaking in the same language
**Then** the bot stays consistent in that language for the rest of the session (no language drift).

**Given** the bot is speaking
**When** I start talking over it
**Then** the bot stops speaking and listens to me (interruption handling via Silero VAD).

**Given** I stop speaking
**When** ~0.8 seconds of silence pass
**Then** the bot considers my turn complete and starts processing (Silero VAD `stop_secs=0.8`).

**Given** any error occurs (API timeout, mic not found, etc.)
**Then** the error is logged via loguru with a clear message, and the bot does not crash silently.

### Technical scope (in)

- Pipecat pipeline: `LocalAudioTransport` → Silero VAD → Whisper STT → Claude LLM → OpenAI TTS → `LocalAudioTransport`
- System prompt that defines Voxtera as a tourism assistant and enforces same-language replies (see `src/voxtera/prompts/system_prompt.py`)
- `.env` loading via `python-dotenv` for `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`
- Loguru logging at `INFO` for: pipeline start, detected language per turn, errors
- Async/await throughout, type hints on all functions
- Single `bot.py` entry point, runnable via `python -m voxtera.bot`

### Technical scope (out — explicitly deferred)

- Daily.co / WebRTC transport (VOX-E2)
- Google Chirp 3 HD TTS (VOX-E3 — using OpenAI TTS as placeholder)
- Dynamic voice switching per language (VOX-E4 — depends on Chirp 3 HD)
- RAG layer for tourism knowledge (VOX-E5)
- Twilio phone integration (VOX-E7)
- DigitalOcean deployment, Docker, Nginx (VOX-E6)
- Admin dashboard, CRM webhooks (VOX-E8, VOX-E9)

### Definition of done

1. `python -m voxtera.bot` (or `make run`) starts cleanly.
2. A live demo conversation works in at least 3 languages (e.g. English, French, Japanese).
3. Logs show the detected language for each user turn.
4. Interruption works (user can talk over the bot).
5. Code is committed with `README.md` setup instructions and a `.env.example`.
6. End-of-user-speech → start-of-bot-speech latency is under ~3 seconds on a normal laptop.

### Dependencies

- OpenAI + Anthropic API keys
- Python 3.12+
- Working microphone and speakers
- Packages: `pipecat-ai[anthropic,openai,silero,local]`, `loguru`, `python-dotenv`

### Effort

0.5–1 day for an experienced Python dev. Most time goes to PortAudio install quirks and tuning the system prompt against language drift.

### Risks / things to watch

- Mic permissions on macOS may require granting Terminal access in System Settings.
- PortAudio install: `brew install portaudio` (macOS) or `apt install libportaudio2` (Ubuntu).
- Language drift — Claude may default to English mid-conversation if the system prompt isn't strict enough; this is the main thing to test.
- Whisper API latency can spike to 2–4s on long utterances; acceptable for now, will revisit when streaming STT is added.

---

## VOX-6 implementation notes

Initial implementation in `src/voxtera/bot.py`. Key choices and where to change them:

| Concern | Default | Where | Notes |
|---|---|---|---|
| LLM model | `claude-haiku-4-5-20251001` | `LLM_MODEL` constant in `bot.py` | Swap to `claude-sonnet-4-5` for higher quality at higher latency. |
| STT model | `whisper-1` (OpenAI API) | `STT_MODEL` constant | Auto language detection; not streaming. |
| TTS model | `tts-1` | `TTS_MODEL` constant | `tts-1-hd` is higher quality but slower. |
| TTS voice | `nova` | `DEFAULT_TTS_VOICE` in `.env` | OpenAI TTS voices: `alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`. |
| VAD stop | 0.8s | `VAD_STOP_SECS` in `.env` | Lower = snappier turn-taking, more risk of cutting off. |
| VAD min volume | 0.02 | `VAD_MIN_VOLUME` in `.env` | RMS floor for "speech vs silence". Tuned per-mic — a built-in MacBook mic is happy at 0.02. |
| VAD confidence | 0.5 | `VAD_CONFIDENCE` in `.env` | Silero model confidence threshold for "this is voice". Drop toward 0.2 if VAD doesn't fire on real speech; raise toward 0.7 if it triggers on noise. |
| Greeting language | `auto` | `GREETING_LANGUAGE` in `.env` | `auto` detects from OS locale; explicit code (`en`, `fr`, `ja`, `ro`, etc.) overrides. Supported codes: en, fr, es, it, de, pt, nl, ja, zh, ko, ar, ru. |
| **Input mode** | `hybrid` | `INPUT_MODE` in `.env` | `voice` = mic only, `text` = keyboard only (mic disabled — useful in libraries / quiet places), `hybrid` = both. Bot reply always plays through speakers/headphones. |
| Bot transcript log | Logs full bot reply per turn | `PipelineTracer` in `bot.py` | The `[voxtera] bot replied (thought Xms): '...'` line is built from accumulated `LLMTextFrame` chunks. |
| Latency timing | `total latency Xms` per turn | `PipelineTracer` in `bot.py` | Measured from `VADUserStoppedSpeakingFrame` (or keyboard Enter) to `BotStartedSpeakingFrame`. Compare against the ~3s VOX-6 acceptance criterion. |

### Likely first-run snags

- **Mic permission (macOS)**: System Settings → Privacy & Security → Microphone → tick your terminal. May need a full quit + relaunch.
- **PortAudio missing**: `brew install portaudio` on macOS, `apt install portaudio19-dev` on Ubuntu.
- **Pipecat import errors**: Pipecat is pre-1.0 and import paths shift between versions. If `make run` errors with `ImportError: cannot import name 'X' from 'pipecat...'`, that's the version. Either pin a working version in `pyproject.toml` or update the import paths to match installed Pipecat.
- **Language drift mid-conversation**: the system prompt currently instructs Claude to reply in the language of the **most recent** user turn (overriding any earlier conversation language). If the bot still drifts, the next escalation is per-turn explicit detection (e.g. `langdetect` on the transcript) plus a forcing prefix in the LLM context, or upgrading from Haiku to Sonnet which follows nuanced instructions more reliably.
