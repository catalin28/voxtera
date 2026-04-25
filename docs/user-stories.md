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
