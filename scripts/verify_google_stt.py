"""Verify Google STT v2 model compatibility with Voxtera's multi-language config.

Run from the project root::

    uv run python scripts/verify_google_stt.py path/to/audio/

Use this as a sanity check before changing ``STT_MODEL_GOOGLE`` in
``src/voxtera/stt.py`` (e.g. swapping ``latest_long`` for ``latest_short``).
The script mirrors the production builder's settings:

- ``languages = ["en-US", "es-ES", "fr-FR"]`` (matches ``_GOOGLE_AUTO_LANGUAGES``)
- ``enable_interim_results = True``
- ``enable_voice_activity_events = True``
- ``enable_automatic_punctuation = False``
- ``location = "global"``

For each WAV provided, runs streaming recognize against each requested model
and prints the transcript, detected language, and wall-clock time. Side-by-side
comparison makes it obvious whether the candidate model produces transcripts
of comparable quality to the incumbent.

WAVs must be **16 kHz, mono, 16-bit PCM** (Voxtera's mic capture format). If
yours aren't, convert first with::

    ffmpeg -i input.wav -ar 16000 -ac 1 -sample_fmt s16 output.wav

The script reads ``GOOGLE_APPLICATION_CREDENTIALS`` from ``.env`` at the
project root. Use the same service-account key the bot uses, so you're
testing what production will actually see.

Exit code:
- 0 if every (file × model) combination returned a non-empty transcript
- 1 if any returned empty / errored — investigate before flipping the model
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
from collections.abc import Iterable
from pathlib import Path

from dotenv import load_dotenv
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import (
    ExplicitDecodingConfig,
    RecognitionConfig,
    RecognitionFeatures,
    StreamingRecognitionConfig,
    StreamingRecognitionFeatures,
    StreamingRecognizeRequest,
)

# Must match _GOOGLE_AUTO_LANGUAGES in src/voxtera/stt.py. Keep them in sync.
LANGUAGE_CODES = ["en-US", "es-ES", "fr-FR"]

# Chunk size matching what Pipecat's GoogleSTTService sends in production:
# 16 kHz × 16-bit mono × 100 ms = 3200 bytes per chunk.
CHUNK_BYTES = 3200


def load_wav(path: Path) -> bytes:
    """Read a 16 kHz mono 16-bit PCM WAV and return raw PCM bytes.

    Raises :class:`ValueError` with a clear conversion hint if the format
    doesn't match what production expects.
    """
    with wave.open(str(path), "rb") as wf:
        ch, sw, fr = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        if (ch, sw, fr) != (1, 2, 16000):
            raise ValueError(
                f"{path.name}: expected 16 kHz mono 16-bit, got "
                f"{fr} Hz, {ch} ch, {sw * 8}-bit. Convert with: "
                f"ffmpeg -i {path.name} -ar 16000 -ac 1 -sample_fmt s16 fixed.wav"
            )
        return wf.readframes(wf.getnframes())


def chunks(data: bytes, size: int) -> Iterable[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


def recognize_streaming(
    client: SpeechClient,
    project_id: str,
    audio_bytes: bytes,
    model: str,
) -> tuple[float, str, str]:
    """Stream ``audio_bytes`` through Google STT v2 and return the final result.

    Returns ``(elapsed_ms, detected_language, transcript)``. ``elapsed_ms`` is
    wall-clock time from request start to the final transcript event — useful
    for relative comparison across models but NOT directly comparable to the
    production "STT bar" (which measures VAD-stop → final, not full duration).
    """
    recognizer = f"projects/{project_id}/locations/global/recognizers/_"

    config = RecognitionConfig(
        explicit_decoding_config=ExplicitDecodingConfig(
            encoding=ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            audio_channel_count=1,
        ),
        language_codes=LANGUAGE_CODES,
        model=model,
        features=RecognitionFeatures(enable_automatic_punctuation=False),
    )
    streaming_config = StreamingRecognitionConfig(
        config=config,
        streaming_features=StreamingRecognitionFeatures(
            interim_results=True,
            enable_voice_activity_events=True,
        ),
    )

    def request_gen() -> Iterable[StreamingRecognizeRequest]:
        yield StreamingRecognizeRequest(recognizer=recognizer, streaming_config=streaming_config)
        for chunk in chunks(audio_bytes, CHUNK_BYTES):
            yield StreamingRecognizeRequest(audio=chunk)

    start = time.monotonic()
    responses = client.streaming_recognize(requests=request_gen())

    final_transcript = ""
    final_lang = ""
    for response in responses:
        for result in response.results:
            if result.is_final and result.alternatives:
                final_transcript = result.alternatives[0].transcript.strip()
                final_lang = result.language_code or ""

    elapsed_ms = (time.monotonic() - start) * 1000
    return elapsed_ms, final_lang, final_transcript


def resolve_project_id(explicit: str | None) -> str:
    """Get the GCP project ID from --project, the env var, or the creds file."""
    if explicit:
        return explicit
    explicit_env = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if explicit_env:
        return explicit_env
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        raise SystemExit(
            "ERROR: no project ID available. Set GOOGLE_APPLICATION_CREDENTIALS "
            "in .env (pointing at a service-account JSON), or pass --project."
        )
    try:
        with open(creds_path) as f:
            return json.load(f)["project_id"]
    except Exception as exc:
        raise SystemExit(f"ERROR: could not read project_id from {creds_path}: {exc}") from exc


def collect_wavs(paths: list[str]) -> list[Path]:
    """Expand directories into their .wav children; keep files as-is."""
    out: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(sorted(path.glob("*.wav")))
        elif path.is_file():
            out.append(path)
        else:
            print(f"WARN: {p} not found, skipping", file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify Google STT v2 models work with Voxtera's multi-language "
            "config before changing STT_MODEL_GOOGLE in src/voxtera/stt.py."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="WAV file(s) or directory of WAVs (16 kHz mono 16-bit PCM)",
    )
    parser.add_argument(
        "--models",
        default="latest_short,latest_long",
        help="Comma-separated Google STT model names (default: %(default)s)",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="GCP project ID (defaults to credentials file's project_id)",
    )
    args = parser.parse_args()

    # Load .env from the project root so this works regardless of cwd.
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    wav_files = collect_wavs(args.paths)
    if not wav_files:
        print("ERROR: no WAV files found in the given paths", file=sys.stderr)
        return 1

    project_id = resolve_project_id(args.project)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"Project: {project_id}")
    print(f"Models:  {', '.join(models)}")
    print(f"Files:   {len(wav_files)}")
    print(f"Languages: {', '.join(LANGUAGE_CODES)}\n")

    client = SpeechClient()
    rows: list[tuple[str, str, float, str, str]] = []

    for wav_path in wav_files:
        try:
            audio = load_wav(wav_path)
        except ValueError as exc:
            print(f"SKIP  {wav_path.name}: {exc}")
            continue
        for model in models:
            print(f"  → {wav_path.name} × {model:<14} ", end="", flush=True)
            try:
                elapsed_ms, lang, transcript = recognize_streaming(client, project_id, audio, model)
                status = "OK   " if transcript else "EMPTY"
                rows.append((wav_path.name, model, elapsed_ms, lang, transcript))
                print(f"{status} {elapsed_ms:>6.0f}ms  lang={lang or '—':<6}  {transcript!r}")
            except Exception as exc:
                rows.append((wav_path.name, model, 0.0, "", f"ERROR: {exc}"))
                print(f"ERROR: {exc}")

    # Compact side-by-side summary.
    print("\n=== Summary ===")
    print(f"{'File':<26} {'Model':<14} {'Time':>9} {'Lang':<8} Transcript")
    print("-" * 100)
    for file, model, elapsed_ms, lang, transcript in rows:
        time_str = f"{elapsed_ms:.0f}ms" if elapsed_ms > 0 else "—"
        trunc = transcript if len(transcript) <= 50 else transcript[:47] + "..."
        print(f"{file:<26} {model:<14} {time_str:>9} {lang or '—':<8} {trunc}")

    any_bad = any(not t or t.startswith("ERROR:") for _, _, _, _, t in rows)
    if any_bad:
        print(
            "\nFAIL: at least one combination returned empty / errored. "
            "Investigate before changing STT_MODEL_GOOGLE."
        )
        return 1

    print("\nPASS: all combinations returned non-empty transcripts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
