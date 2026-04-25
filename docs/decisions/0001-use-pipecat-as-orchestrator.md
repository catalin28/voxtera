# ADR-0001: Use Pipecat as the voice-pipeline orchestrator

- **Date:** 2026-04-25
- **Status:** Accepted
- **Deciders:** Voxtera architect, lead developer

## Context

Voxtera needs an async, streaming pipeline that wires together STT, an LLM, and TTS, plus voice-activity detection and interruption handling. Building this from scratch with raw asyncio + websockets would burn weeks before we had a demo.

## Decision

Use [Pipecat](https://github.com/pipecat-ai/pipecat) as the orchestration framework, with the `[anthropic, openai, silero, local]` extras for Sprint 1. We'll add `[daily]` once we move to WebRTC (VOX-E2).

## Consequences

- We get streaming pipelines, VAD, interruption, and transports out of the box.
- Tradeoff: we inherit Pipecat's async model and frame-passing conventions; the team needs to learn them.
- The framework is young (pre-1.0), so APIs may shift; we'll pin to a known-good version in `pyproject.toml`.

## Alternatives considered

- **LiveKit Agents** — strong WebRTC story, but Sprint 1 is local-only, and Pipecat's STT/TTS service catalogue is broader.
- **Roll our own** — too slow; orchestrator is undifferentiated work.

## References

- Kickoff doc §5.3.
