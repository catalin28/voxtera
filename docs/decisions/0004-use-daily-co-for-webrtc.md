# ADR-0004: Use Daily.co for WebRTC transport

- **Date:** 2026-04-25
- **Status:** Proposed (target: VOX-E2)
- **Deciders:** Voxtera architect

## Context

Once Sprint 1's local laptop demo works, we need real-time voice over WebRTC for browser and mobile clients. Pipecat already integrates with Daily.co.

## Decision

Plan to adopt **Daily.co** for WebRTC when VOX-E2 is picked up. The Pipecat `[daily]` extra makes the swap from `LocalAudioTransport` to Daily transport mostly mechanical.

## Consequences

- We avoid running our own SFU.
- Adds one more vendor with its own pricing and key management.
- We inherit Daily's regional infrastructure for low-latency routing.

## Alternatives considered

- **LiveKit Cloud** — solid alternative; Pipecat support is good. Daily.co was chosen for tighter integration and the team's prior familiarity.
- **Self-hosted SFU (mediasoup, ion-sfu)** — too much ops burden for this stage.

## References

- Kickoff doc §11 — VOX-E2.
