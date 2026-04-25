# ADR-0002: Use OpenAI Whisper over Deepgram Flux for STT

- **Date:** 2026-04-25
- **Status:** Accepted
- **Deciders:** Voxtera architect

## Context

Voxtera must auto-detect the user's language from speech. Sprint 1 is a local laptop demo; production-grade streaming latency isn't required yet.

## Decision

Use **OpenAI Whisper** via the OpenAI API for Sprint 1. Revisit Deepgram Flux (or another streaming STT) when we add real-time streaming as a follow-up.

## Consequences

- Strong multilingual coverage and language detection out of the box.
- Single OpenAI account already covers STT and the placeholder TTS.
- Tradeoff: not streaming — full-utterance latency can spike to 2–4s on long inputs. Acceptable for the demo, will be revisited.

## Alternatives considered

- **Deepgram Flux** — true streaming, lower per-token latency, but less mature multilingual coverage at the time of decision.
- **AssemblyAI** — good quality, but adds another vendor for marginal benefit.

## References

- Kickoff doc §11 risks ("Whisper API latency can spike to 2–4s").
