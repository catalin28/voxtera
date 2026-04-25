# Voxtera Architecture

> Status: high-level overview for Sprint 1. The full diagram and service list will be filled in by the architect.

## Goal

A multilingual real-time voice agent for the tourism industry. The bot detects the user's spoken language and replies in that same language with low latency.

## Pipeline (Sprint 1 — local voice loop, VOX-6)

```
Microphone
   │
   ▼
LocalAudioTransport (Pipecat)
   │
   ▼
Silero VAD  ──── stop_secs = 0.8s, supports interruption
   │
   ▼
OpenAI Whisper STT  ──── auto language detection
   │
   ▼
Anthropic Claude (LLM)  ──── system prompt enforces same-language replies
   │
   ▼
OpenAI TTS  ──── placeholder voice (e.g. "nova"), language-agnostic
   │
   ▼
LocalAudioTransport
   │
   ▼
Speakers
```

All stages are async and stream where possible. Loguru handles structured logging.

## Future stages (deferred)

| Phase | Change                                                | Tracking epic |
|-------|-------------------------------------------------------|---------------|
| 2     | Replace `LocalAudioTransport` with Daily.co WebRTC    | VOX-E2        |
| 3     | Replace OpenAI TTS with Google Chirp 3 HD             | VOX-E3        |
| 4     | Switch TTS voice dynamically per detected language    | VOX-E4        |
| 5     | Add RAG layer for tourism knowledge                   | VOX-E5        |
| 6     | Deploy to DigitalOcean (Docker + Nginx)               | VOX-E6        |
| 7     | Twilio phone integration                              | VOX-E7        |
| 8     | Admin dashboard                                       | VOX-E8        |
| 9     | CRM webhook integration                               | VOX-E9        |

## External services and SDKs

- **Anthropic API** — Claude (LLM)
- **OpenAI API** — Whisper (STT), TTS
- **Pipecat** — orchestration framework, with extras `[anthropic, openai, silero, local]`
- **Silero VAD** — voice activity detection
- **Loguru** — logging
- **python-dotenv** — env loading

## Security

- API keys live in `.env` (never committed) and the team password manager.
- Production keys will live in CI secrets and on-server `.env` later — see [`docs/handoff.md`](handoff.md) §"Secrets management".
