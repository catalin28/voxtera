#!/usr/bin/env python3
"""Generate a subtle lobby room-tone loop for the call's background mixer.

Synthesizes a warm, low-level ambience (shaped noise with slow movement —
reads as air-conditioning hum + distant room activity) rather than shipping
a copyrighted recording. 30 s, seamless loop, 24 kHz mono 16-bit — exactly
what ``SoundfileMixer`` wants at the call transport's output rate.

Replace ``assets/audio/lobby_tone.wav`` with a real (licensed) lobby
recording any time — same format, the mixer doesn't care.

Usage:  uv run python scripts/generate_ambience.py
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 24000
SECONDS = 30


def main() -> None:
    rng = np.random.default_rng(7)
    n = SAMPLE_RATE * SECONDS

    # Brown-ish noise: integrate white noise, then high-pass slightly so it
    # doesn't rumble. This reads as distant HVAC / room air.
    white = rng.standard_normal(n)
    brown = np.cumsum(white)
    brown -= np.linspace(brown[0], brown[-1], n)  # detrend → seamless loop
    # One-pole high-pass (~80 Hz) to remove inaudible-but-power-hungry lows.
    alpha = 1.0 - 2 * np.pi * 80 / SAMPLE_RATE
    hp = np.empty_like(brown)
    prev_in = prev_out = 0.0
    for i, x in enumerate(brown):
        prev_out = alpha * (prev_out + x - prev_in)
        prev_in = x
        hp[i] = brown[i] = prev_out

    # Slow amplitude movement (two incommensurate LFOs) so it breathes like a
    # space with people in it instead of a constant hiss. Full periods over
    # the loop length keep the loop seamless.
    t = np.arange(n) / SAMPLE_RATE
    lfo = (
        1.0
        + 0.18 * np.sin(2 * np.pi * 3 / SECONDS * t)
        + 0.12 * np.sin(2 * np.pi * 7 / SECONDS * t + 1.3)
    )
    tone = hp * lfo

    tone /= np.max(np.abs(tone))
    # Headroom: the mixer applies its own volume, but bake in a low ceiling so
    # even volume=1.0 misconfiguration stays survivable.
    pcm = (tone * 0.35 * 32767).astype(np.int16)

    out = Path(__file__).resolve().parents[1] / "assets" / "audio"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "lobby_tone.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    print(f"wrote {path} ({SECONDS}s, {SAMPLE_RATE} Hz, seamless loop)")


if __name__ == "__main__":
    main()
