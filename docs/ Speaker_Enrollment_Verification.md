# Voxtera — Speaker Enrollment & Verification (Layer 4)

**Audience:** LLM coder implementing the feature.
**Project:** Voxtera — multilingual real-time voice agent for tourism.
**Stack:** Python 3.12+, Pipecat (>= 0.0.85), Daily.co WebRTC, Silero VAD, OpenAI Whisper STT, Anthropic Claude Sonnet 4.6, Google Cloud TTS Chirp 3 HD.

---

## 1. Problem Statement

The bot currently transcribes any speech detected in the room — including background voices (other people physically near the user, TV, distant chatter). Whisper has no concept of "who" is speaking, and Silero VAD only detects "is this speech." This causes the bot to pick up and respond to non-user voices when the user is silent.

Layers 1–3 (browser audio constraints, Daily Krisp, VAD tuning) are already in place and reduce the issue but do not eliminate it. Layer 4 (this spec) is the durable fix: identify the user's voice at the start of the call and verify every subsequent utterance comes from the same speaker.

---

## 2. Goal

Implement a `SpeakerVerificationProcessor` that:

1. **Enrolls** the user's voice from their first utterance in the call.
2. **Verifies** every subsequent utterance against the enrolled embedding before forwarding it to Whisper.
3. **Drops** audio that does not match the enrolled speaker (background voices, other people, TV).

The processor must be inserted into the Pipecat pipeline between the VAD analyzer and the STT service.

---

## 3. Architecture

### Current pipeline (simplified)

```
DailyTransport (in)
   → SileroVADAnalyzer
   → WhisperSTTService
   → ContextAggregator (user)
   → ClaudeLLMService
   → GoogleTTSService
   → DailyTransport (out)
```

### New pipeline

```
DailyTransport (in)
   → SileroVADAnalyzer
   → SpeakerVerificationProcessor   ← NEW
   → WhisperSTTService
   → ContextAggregator (user)
   → ClaudeLLMService
   → GoogleTTSService
   → DailyTransport (out)
```

### Behavior of `SpeakerVerificationProcessor`

State machine, per session:

| State | Behavior |
|---|---|
| `AWAITING_ENROLLMENT` | Buffer audio between `UserStartedSpeakingFrame` and `UserStoppedSpeakingFrame`. On stop, compute embedding from the buffer. If buffer ≥ `MIN_ENROLLMENT_DURATION` seconds, store as enrolled embedding and transition to `VERIFYING`. Forward all buffered audio frames downstream (the user's first utterance is always trusted — the bot needs it to bootstrap the conversation). |
| `VERIFYING` | Buffer audio between `UserStartedSpeakingFrame` and `UserStoppedSpeakingFrame`. On stop, compute embedding and compare to enrolled embedding via cosine similarity. If `similarity ≥ SIMILARITY_THRESHOLD`, forward all buffered audio frames downstream. Otherwise, drop them silently and log the rejection. |

The processor must NEVER block or delay control frames (system frames, end-of-pipeline frames). Only `AudioRawFrame` instances should be buffered/dropped.

---

## 4. Dependencies

Add to `requirements.txt`:

```
resemblyzer>=0.1.4
numpy>=1.24
librosa>=0.10  # required by resemblyzer for resampling
```

Notes:
- `resemblyzer` ships its pretrained ECAPA-style model (~17 MB). No external model download needed at runtime — model loads on first instantiation.
- CPU-only inference is fast enough (~30–80 ms per utterance on a 2 vCPU Droplet). No GPU required.
- `librosa` is a transitive dependency for audio resampling. Keep it pinned to avoid breakage.

---

## 5. Configuration

Add the following to `.env.example` and load via `python-dotenv`:

```
# Speaker verification (Layer 4)
SPEAKER_VERIFICATION_ENABLED=true
SPEAKER_SIMILARITY_THRESHOLD=0.70   # cosine similarity, range 0.0–1.0
SPEAKER_MIN_ENROLLMENT_SECS=2.0     # minimum audio length to enroll
SPEAKER_MAX_BUFFER_SECS=30.0        # safety cap to prevent OOM on long monologues
```

All thresholds must be readable from environment variables, with the values above as defaults. Do not hardcode.

---

## 6. File-by-File Implementation

### 6.1 New file: `processors/__init__.py`

Empty file to mark `processors/` as a Python package.

### 6.2 New file: `processors/speaker_verification.py`

Full implementation of the processor. Skeleton:

```python
"""Speaker enrollment and verification for Voxtera.

Captures the user's voice on their first utterance and verifies all
subsequent utterances against that enrolled embedding. Audio that does
not match is dropped before reaching Whisper STT.
"""

import os
from enum import Enum
from typing import Optional

import numpy as np
from loguru import logger
from resemblyzer import VoiceEncoder

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class SpeakerState(Enum):
    AWAITING_ENROLLMENT = "awaiting_enrollment"
    VERIFYING = "verifying"


class SpeakerVerificationProcessor(FrameProcessor):
    """Verifies inbound audio matches the enrolled user's voice.

    Inserted between the VAD analyzer and the STT service. Buffers audio
    frames during a VAD-detected turn and, on turn end, compares the
    speaker embedding to the one captured during enrollment. Forwards
    audio downstream only on a match.
    """

    EXPECTED_SAMPLE_RATE = 16_000  # resemblyzer requirement

    def __init__(
        self,
        similarity_threshold: float = 0.70,
        min_enrollment_secs: float = 2.0,
        max_buffer_secs: float = 30.0,
    ) -> None:
        super().__init__()
        self._encoder = VoiceEncoder(verbose=False)
        self._threshold = similarity_threshold
        self._min_enrollment_secs = min_enrollment_secs
        self._max_buffer_secs = max_buffer_secs

        self._state = SpeakerState.AWAITING_ENROLLMENT
        self._enrolled_embedding: Optional[np.ndarray] = None
        self._buffer: list[AudioRawFrame] = []
        self._buffered_samples = 0
        self._is_speaking = False

        logger.info(
            "SpeakerVerificationProcessor initialized "
            f"(threshold={self._threshold}, min_enroll={self._min_enrollment_secs}s)"
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Forward all non-audio control frames immediately
        if isinstance(frame, UserStartedSpeakingFrame):
            self._is_speaking = True
            self._buffer.clear()
            self._buffered_samples = 0
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._is_speaking = False
            await self._handle_turn_end(frame, direction)
            return

        # Audio frames during an active turn are buffered, not forwarded yet
        if isinstance(frame, AudioRawFrame) and self._is_speaking:
            self._buffer.append(frame)
            self._buffered_samples += len(frame.audio) // 2  # int16 = 2 bytes/sample

            # Safety cap
            if self._buffered_samples / self.EXPECTED_SAMPLE_RATE > self._max_buffer_secs:
                logger.warning("Audio buffer exceeded max size, flushing")
                self._buffer.clear()
                self._buffered_samples = 0
            return

        # All other frames: pass through
        await self.push_frame(frame, direction)

    async def _handle_turn_end(
        self, stop_frame: UserStoppedSpeakingFrame, direction: FrameDirection
    ) -> None:
        """Compute embedding from buffer; enroll or verify; forward or drop."""
        if not self._buffer:
            await self.push_frame(stop_frame, direction)
            return

        audio_np = self._buffer_to_float32()
        duration_secs = len(audio_np) / self.EXPECTED_SAMPLE_RATE

        if self._state == SpeakerState.AWAITING_ENROLLMENT:
            if duration_secs < self._min_enrollment_secs:
                logger.info(
                    f"Enrollment audio too short ({duration_secs:.2f}s < "
                    f"{self._min_enrollment_secs}s), forwarding without enrolling"
                )
                await self._flush_buffer(direction)
                await self.push_frame(stop_frame, direction)
                return

            self._enrolled_embedding = self._encoder.embed_utterance(audio_np)
            self._state = SpeakerState.VERIFYING
            logger.info(
                f"Enrolled user voice from {duration_secs:.2f}s of audio. "
                "Verification active for subsequent utterances."
            )
            await self._flush_buffer(direction)
            await self.push_frame(stop_frame, direction)
            return

        # VERIFYING state
        candidate_embedding = self._encoder.embed_utterance(audio_np)
        similarity = self._cosine_similarity(
            self._enrolled_embedding, candidate_embedding
        )

        if similarity >= self._threshold:
            logger.debug(
                f"Speaker verified (similarity={similarity:.3f} >= {self._threshold})"
            )
            await self._flush_buffer(direction)
            await self.push_frame(stop_frame, direction)
        else:
            logger.info(
                f"Speaker rejected (similarity={similarity:.3f} < {self._threshold}), "
                f"dropping {duration_secs:.2f}s of audio"
            )
            self._buffer.clear()
            self._buffered_samples = 0
            # Do NOT forward stop_frame — downstream STT/LLM never sees the turn

    async def _flush_buffer(self, direction: FrameDirection) -> None:
        for audio_frame in self._buffer:
            await self.push_frame(audio_frame, direction)
        self._buffer.clear()
        self._buffered_samples = 0

    def _buffer_to_float32(self) -> np.ndarray:
        """Concatenate buffered AudioRawFrames into a single float32 array
        in [-1.0, 1.0], as required by resemblyzer."""
        raw_bytes = b"".join(f.audio for f in self._buffer)
        int16 = np.frombuffer(raw_bytes, dtype=np.int16)
        return int16.astype(np.float32) / 32768.0

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Resemblyzer embeddings are L2-normalized, so dot product == cosine."""
        return float(np.dot(a, b))
```

### 6.3 Modify: `bot.py`

Wire the processor into the existing pipeline. Locate the `Pipeline([...])` construction and insert `SpeakerVerificationProcessor` immediately after the VAD-aware transport input and before the STT service.

```python
import os
from processors.speaker_verification import SpeakerVerificationProcessor

# ... existing imports and setup ...

speaker_verifier = None
if os.getenv("SPEAKER_VERIFICATION_ENABLED", "true").lower() == "true":
    speaker_verifier = SpeakerVerificationProcessor(
        similarity_threshold=float(os.getenv("SPEAKER_SIMILARITY_THRESHOLD", "0.70")),
        min_enrollment_secs=float(os.getenv("SPEAKER_MIN_ENROLLMENT_SECS", "2.0")),
        max_buffer_secs=float(os.getenv("SPEAKER_MAX_BUFFER_SECS", "30.0")),
    )

pipeline_steps = [
    transport.input(),
    # speaker verification BEFORE STT — drop bad audio before transcription cost
]
if speaker_verifier:
    pipeline_steps.append(speaker_verifier)

pipeline_steps.extend([
    stt,
    context_aggregator.user(),
    llm,
    tts,
    transport.output(),
    context_aggregator.assistant(),
])

pipeline = Pipeline(pipeline_steps)
```

The feature must be toggleable via `SPEAKER_VERIFICATION_ENABLED` so it can be disabled without code changes if it causes regressions in production.

---

## 7. Testing

### 7.1 Unit test: `tests/test_speaker_verification.py`

Mock VoiceEncoder to return deterministic embeddings. Cover:

1. Enrollment happens on first utterance ≥ `min_enrollment_secs`.
2. Enrollment is skipped when first utterance < `min_enrollment_secs` (audio is forwarded anyway).
3. Subsequent utterance with similar embedding → forwarded.
4. Subsequent utterance with dissimilar embedding → dropped (no `UserStoppedSpeakingFrame` reaches downstream).
5. Buffer is cleared and reset between turns.
6. Max buffer cap prevents unbounded memory growth.

### 7.2 Integration test: live call

1. Start the bot, join the Daily room from a browser.
2. Speak the greeting reply ("Hi, I'm planning a trip to Lisbon"). Verify logs show "Enrolled user voice from X seconds".
3. Have a second person speak (or play audio of a different voice from a phone). Verify logs show "Speaker rejected" and the bot does NOT respond.
4. Resume speaking yourself. Verify logs show "Speaker verified" and the bot responds normally.
5. Tune `SPEAKER_SIMILARITY_THRESHOLD` if you observe false rejections of the enrolled user (lower it) or false acceptances of other voices (raise it). Sweet spot is usually 0.65–0.75.

### 7.3 Acceptance criteria

- [ ] First user utterance is always transcribed and acted on (bootstrap works).
- [ ] When a non-enrolled voice speaks, the bot does not respond and the log shows a rejection with similarity score.
- [ ] When the enrolled user speaks, behavior is identical to the pre-Layer-4 system.
- [ ] Disabling via `SPEAKER_VERIFICATION_ENABLED=false` restores the old behavior with no code changes.
- [ ] Per-utterance latency overhead is < 150 ms on a 2 vCPU Droplet.
- [ ] No memory leaks across a 30-minute call (verify `_buffer` and `_buffered_samples` reset every turn).

---

## 8. Edge Cases

| Case | Required behavior |
|---|---|
| User says nothing during the first VAD turn (e.g., picks up a cough). | Audio < `min_enrollment_secs` → forward without enrolling, stay in `AWAITING_ENROLLMENT`. Next valid utterance will enroll. |
| User's voice changes mid-call (cold, headset swap). | Out of scope for v1. Consider a future "re-enrollment on N consecutive rejections" mechanism. |
| Two users in the room both want to talk. | Out of scope for v1. The first speaker wins; second speaker is treated as background. Document this in the widget UX as "single-user mode." |
| VAD never triggers (silent line). | Processor stays in `AWAITING_ENROLLMENT` indefinitely. No action needed — no audio means no work. |
| `resemblyzer` model fails to load at startup. | Log error and disable verification (forward all audio unmodified). The bot must remain functional even if speaker verification is unavailable. |
| Phone-bridged calls (8 kHz codec). | Resemblyzer requires 16 kHz. Pipecat's `DailyTransport` should already resample, but verify in integration testing once Twilio/PSTN integration lands (priority #3 in roadmap). |

---

## 9. Logging Requirements

Use `loguru` (already in the project). Required log lines:

- `INFO`: Processor initialized with config values.
- `INFO`: Enrollment success — duration of audio used.
- `INFO`: Enrollment skipped (audio too short).
- `DEBUG`: Speaker verified — similarity score.
- `INFO`: Speaker rejected — similarity score and rejected duration.
- `WARNING`: Buffer cap exceeded.
- `ERROR`: Model load failure (with fallback to passthrough mode).

Log similarity scores as `f"{score:.3f}"` for tuning visibility.

---

## 10. Out of Scope (Future Work)

- Multi-speaker support (group tours, families on speakerphone).
- Re-enrollment on extended rejection streaks.
- Speaker diarization (timestamping who-said-what for analytics).
- Anti-spoofing (detection of recorded/replayed audio).
- Pyannote upgrade for higher-accuracy verification (stick with resemblyzer until accuracy proves insufficient in the field).

---

## 11. Done Definition

- All files in section 6 created/modified.
- Unit tests in section 7.1 pass.
- Integration test in section 7.2 demonstrates rejection of a non-enrolled voice on a live Daily call.
- `.env.example` updated with new variables and comments.
- `requirements.txt` updated with the three new dependencies.
- Brief usage note added to the project README under a "Speaker Verification" heading.
