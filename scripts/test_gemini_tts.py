#!/usr/bin/env python3
"""Isolated Gemini-TTS smoke test.

Runs the exact same Cloud Text-to-Speech call the admin voice-preview uses,
so you see the *raw* error (SERVICE_DISABLED vs PERMISSION_DENIED vs OK)
without the admin UI wrapping it in "preview_failed".

Usage (from repo root, with the venv active and .env loaded):
    python scripts/test_gemini_tts.py
"""

import os
import sys
from pathlib import Path

# Resolve the same service-account creds the app uses.
creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
if creds and not os.path.isabs(creds):
    creds = str(Path(__file__).resolve().parent.parent / creds)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds

print(f"GOOGLE_APPLICATION_CREDENTIALS -> {creds}")
print(f"exists: {Path(creds).exists() if creds else False}")

try:
    from google.cloud import texttospeech
except ImportError:
    sys.exit("google-cloud-texttospeech not installed in this env")

model_name = os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-tts")
voice_name = os.environ.get("GEMINI_TTS_VOICE", "Charon")
prompt = (os.environ.get("GEMINI_TTS_PROMPT") or "").strip()

print(f"model={model_name} voice={voice_name} prompt={'set' if prompt else 'none'}")

client = texttospeech.TextToSpeechClient()
synthesis_input = (
    texttospeech.SynthesisInput(text="Good evening. How can I help you today?", prompt=prompt)
    if prompt
    else texttospeech.SynthesisInput(text="Good evening. How can I help you today?")
)
voice = texttospeech.VoiceSelectionParams(
    language_code="en-US",
    name=voice_name,
    model_name=model_name,
)
audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)

try:
    resp = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    out = Path(__file__).resolve().parent.parent / "gemini_tts_test.mp3"
    out.write_bytes(resp.audio_content)
    print(f"OK — wrote {len(resp.audio_content)} bytes to {out}")
except Exception as exc:  # noqa: BLE001 — we want the raw error class + message
    print(f"\nFAILED: {type(exc).__name__}: {exc}")
    sys.exit(1)
