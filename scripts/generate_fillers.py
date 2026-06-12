#!/usr/bin/env python3
"""Generate the voice-filler WAV clips in the bot's own ElevenLabs voice.

Renders each phrase below with the SAME voice id the bot speaks with
(``ELEVENLABS_VOICE_ID``), at the call transport's output rate (24 kHz mono
16-bit), into ``assets/fillers/<lang>/<nn>.wav``. Re-run after changing the
active voice so the fillers stay indistinguishable from the bot.

Usage:  uv run python scripts/generate_fillers.py  (reads .env)
"""

from __future__ import annotations

import os
import sys
import urllib.request
import wave
from pathlib import Path

from dotenv import load_dotenv

SAMPLE_RATE = 24000  # matches the call bot's audio_out_sample_rate
MODEL = "eleven_flash_v2_5"

# Short, neutral, breath-like — they must work in front of ANY answer.
# Wrapped in unhurried sentence shapes (lowercase starts, commas) so the
# model reads them as quiet asides, not announcements. The first live take
# rendered isolated exclamations with far too much energy.
PHRASES: dict[str, list[str]] = {
    "en": [
        "mm, one moment,",
        "just a second,",
        "let me see,",
        "hmm, let me check,",
    ],
    "tr": [
        "hemen bakıyorum,",
        "bir saniye,",
        "şöyle bakayım,",
    ],
    "fr": [
        "un instant,",
        "voyons voir,",
    ],
    "ro": [
        "o clipă,",
        "să văd,",
    ],
}

# Post-processing: a small leading pause (the "breath"), gentle fades so the
# clip never starts or ends abruptly, and a -6 dB pad — a filler should sit
# UNDER the bot's normal speaking level, like a real aside.
LEAD_SILENCE_MS = 140
FADE_MS = 30
GAIN = 0.5


def _post_process(pcm: bytes) -> bytes:
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    # Trim digital silence at the edges so fades act on actual sound.
    nz = np.flatnonzero(np.abs(samples) > 100)
    if nz.size:
        samples = samples[nz[0] : nz[-1] + 1]
    fade = int(SAMPLE_RATE * FADE_MS / 1000)
    if samples.size > 2 * fade:
        samples[:fade] *= np.linspace(0.0, 1.0, fade)
        samples[-fade:] *= np.linspace(1.0, 0.0, fade)
    samples *= GAIN
    lead = np.zeros(int(SAMPLE_RATE * LEAD_SILENCE_MS / 1000), dtype=np.float32)
    out = np.concatenate([lead, samples])
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


def synthesize(text: str, *, api_key: str, voice_id: str) -> bytes:
    """One ElevenLabs call returning raw 24 kHz PCM."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000"
    body = {
        "text": text,
        "model_id": MODEL,
        # Maximum stability = flattest, calmest delivery. Fillers are asides,
        # not lines; any expressiveness reads as shouting at this length.
        "voice_settings": {"stability": 0.95, "similarity_boost": 0.75},
    }
    import json

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main() -> int:
    load_dotenv()
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if not api_key or not voice_id:
        print("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must be set (.env)")
        return 1

    out_base = Path(__file__).resolve().parents[1] / "assets" / "fillers"
    total = 0
    for lang, phrases in PHRASES.items():
        lang_dir = out_base / lang
        lang_dir.mkdir(parents=True, exist_ok=True)
        for i, phrase in enumerate(phrases, start=1):
            pcm = _post_process(synthesize(phrase, api_key=api_key, voice_id=voice_id))
            path = lang_dir / f"{i:02d}.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm)
            secs = len(pcm) / 2 / SAMPLE_RATE
            print(f"  {path.relative_to(out_base.parent.parent)}  {secs:.2f}s  {phrase!r}")
            total += 1
    print(f"Done — {total} clips (voice {voice_id[:6]}…, {SAMPLE_RATE} Hz).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
