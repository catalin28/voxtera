# ADR-0003: Use Google Chirp 3 HD over ElevenLabs for production TTS

- **Date:** 2026-04-25
- **Status:** Proposed (target: VOX-E3)
- **Deciders:** Voxtera architect

## Context

Sprint 1 uses OpenAI TTS as a placeholder. For production we want a TTS that supports many languages with high-quality voices and low latency, and that lets us pick a different voice per detected language.

## Decision

Plan to adopt **Google Chirp 3 HD** when VOX-E3 is picked up. Until then, OpenAI TTS remains the default.

## Consequences

- Broader language coverage and voice catalogue than ElevenLabs at expected price points.
- Adds a third API vendor (Google Cloud) — IAM, billing, and key rotation procedures need to be added to the runbook.
- Dynamic voice switching per language (VOX-E4) becomes possible.

## Alternatives considered

- **ElevenLabs** — excellent voice quality in English, but per-language voice variety is narrower and pricing scales less favourably.
- **OpenAI TTS** — kept as the Sprint 1 placeholder; not enough language-specific voices for production.

## References

- Kickoff doc §11 — VOX-E3, VOX-E4.
