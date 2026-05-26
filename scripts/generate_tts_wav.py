"""Generate WAV files from text in multiple languages.

Two TTS providers are supported:
    google      — Google Chirp 3 HD (default; needs GOOGLE_APPLICATION_CREDENTIALS)
    elevenlabs  — ElevenLabs Flash v2.5 (needs ELEVENLABS_API_KEY). Uses the same
                  voice as the live demo bot: defaults to ELEVENLABS_VOICE_ID from
                  .env, falling back to Rachel (21m00Tcm4TlvDq8ikWAM).

Usage examples:
    # Google Chirp 3 HD (default)
    uv run python scripts/generate_tts_wav.py --text "Hello, welcome to the hotel" --lang en

    # ElevenLabs with the demo voice
    uv run python scripts/generate_tts_wav.py \
        --text "Hello, welcome to the hotel" --lang en --provider elevenlabs

    # ElevenLabs override voice id
    uv run python scripts/generate_tts_wav.py \
        --text "Hello" --lang en --provider elevenlabs --voice-id pNInz6obpgDQGcFmaJgB

    # Turkish (pre-translated)
    uv run python scripts/generate_tts_wav.py --text "Merhaba, otele hoş geldiniz" --lang tr

    # Auto-translate English to Turkish
    uv run python scripts/generate_tts_wav.py --text "What can I eat today?" --lang tr --translate

    # Auto-translate English to French with a Chirp 3 HD voice character
    uv run python scripts/generate_tts_wav.py \
        --text "Hello, welcome" --lang fr --translate --voice Aoede

    # Custom output path
    uv run python scripts/generate_tts_wav.py \
        --text "Hola, bienvenido" --lang es -o output/spanish_greeting.wav

    # Batch mode — generate WAVs from a JSON file of sentences
    uv run python scripts/generate_tts_wav.py --batch sentences.json

    # Generate for ALL supported languages at once (uses a default phrase per language)
    uv run python scripts/generate_tts_wav.py --all

    # List available languages and voices
    uv run python scripts/generate_tts_wav.py --list

Environment:
    GOOGLE_APPLICATION_CREDENTIALS must point to a valid service account JSON file
        (required when --provider google, which is the default).
    ELEVENLABS_API_KEY required when --provider elevenlabs.
    ELEVENLABS_VOICE_ID (optional) — default voice ID for --provider elevenlabs.
    ELEVENLABS_MODEL (optional) — ElevenLabs model, defaults to eleven_flash_v2_5.
    ANTHROPIC_API_KEY or OPENAI_API_KEY required for --translate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import wave
from pathlib import Path

# Ensure src/ is importable when running from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn text into a safe filename slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:max_len]


def translate_text(text: str, target_lang: str) -> str:
    """Translate English text to the target language using Anthropic or OpenAI."""
    import os

    from dotenv import load_dotenv

    load_dotenv()

    from voxtera.lang_config import translation_name_for

    lang_name = translation_name_for(target_lang)
    prompt = (
        f"Translate the following English text to {lang_name}. "
        f"Return ONLY the translation, nothing else.\n\n{text}"
    )

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if anthropic_key:
        import anthropic

        client = anthropic.Anthropic(api_key=anthropic_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    elif openai_key:
        import openai

        client = openai.OpenAI(api_key=openai_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()
    else:
        print(
            "ERROR: --translate requires ANTHROPIC_API_KEY or OPENAI_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)


def get_google_credentials_path() -> Path:
    """Resolve Google credentials from env or .env file."""
    import os

    from dotenv import load_dotenv

    load_dotenv()
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds:
        print("ERROR: GOOGLE_APPLICATION_CREDENTIALS not set.", file=sys.stderr)
        print("Set it in .env or export it in your shell.", file=sys.stderr)
        sys.exit(1)
    path = Path(creds).expanduser()
    if not path.exists():
        print(f"ERROR: credentials file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return path


# ElevenLabs defaults — kept in sync with src/voxtera/tts.py.
# Rachel: calm, warm American English, ElevenLabs' canonical multilingual demo voice.
_DEFAULT_ELEVENLABS_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
_DEFAULT_ELEVENLABS_MODEL = "eleven_flash_v2_5"


def get_elevenlabs_config(cli_voice_id: str | None = None) -> tuple[str, str, str]:
    """Return (api_key, voice_id, model_id) for ElevenLabs, resolving from env/.env.

    Resolution order for voice_id:
        1. ``--voice-id`` CLI flag if provided
        2. ``ELEVENLABS_VOICE_ID`` env var
        3. Built-in default (Rachel — matches the live demo bot)
    """
    import os

    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set.", file=sys.stderr)
        print("Set it in .env or export it in your shell.", file=sys.stderr)
        sys.exit(1)
    voice_id = cli_voice_id or os.environ.get("ELEVENLABS_VOICE_ID") or _DEFAULT_ELEVENLABS_VOICE_ID
    model_id = os.environ.get("ELEVENLABS_MODEL", _DEFAULT_ELEVENLABS_MODEL)
    return api_key, voice_id, model_id


def synthesize_to_wav(
    text: str,
    locale: str,
    voice_name: str,
    output_path: Path,
    credentials_path: Path,
    sample_rate: int = 24000,
) -> None:
    """Synthesize text to a WAV file using Google Cloud TTS (Chirp 3 HD)."""
    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient.from_service_account_json(str(credentials_path))

    synthesis_input = texttospeech.SynthesisInput(text=text)

    voice_params = texttospeech.VoiceSelectionParams(
        language_code=locale,
        name=voice_name,
    )

    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=sample_rate,
    )

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice_params,
        audio_config=audio_config,
    )

    # Write raw PCM into a proper WAV file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(response.audio_content)

    print(f"  ✓ {output_path} ({len(response.audio_content)} bytes)")


# ElevenLabs supports a fixed set of PCM sample rates via the
# ``output_format=pcm_<rate>`` query parameter. We map the user's
# ``--sample-rate`` to the nearest supported value.
_ELEVENLABS_SUPPORTED_PCM_RATES: tuple[int, ...] = (8000, 16000, 22050, 24000, 44100, 48000)


def _elevenlabs_output_format(sample_rate: int) -> tuple[str, int]:
    """Pick a supported ElevenLabs PCM format for the requested sample rate.

    Returns ``(output_format_string, actual_sample_rate)``. If the requested
    rate isn't supported, snaps to the closest supported value and warns.
    """
    if sample_rate in _ELEVENLABS_SUPPORTED_PCM_RATES:
        return f"pcm_{sample_rate}", sample_rate
    closest = min(_ELEVENLABS_SUPPORTED_PCM_RATES, key=lambda r: abs(r - sample_rate))
    print(
        f"  ! sample_rate={sample_rate} not supported by ElevenLabs; using {closest} instead",
        file=sys.stderr,
    )
    return f"pcm_{closest}", closest


def synthesize_elevenlabs_to_wav(
    text: str,
    lang_code: str,
    voice_id: str,
    model_id: str,
    api_key: str,
    output_path: Path,
    sample_rate: int = 24000,
) -> None:
    """Synthesize text to a WAV file using ElevenLabs (Flash v2.5 by default).

    Calls the REST endpoint directly (no SDK required) and wraps the returned
    raw PCM in a WAV header so the output is a proper playable file.
    """
    import httpx

    output_format, actual_rate = _elevenlabs_output_format(sample_rate)
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    params = {"output_format": output_format}
    headers = {
        "xi-api-key": api_key,
        "accept": "audio/pcm",
        "content-type": "application/json",
    }
    payload: dict[str, object] = {
        "text": text,
        "model_id": model_id,
    }
    # Flash v2.5 / Turbo v2.5 honour an explicit per-request language hint.
    # Older models infer language from the text itself, in which case the
    # field is silently ignored — safe to always send.
    if lang_code:
        payload["language_code"] = lang_code

    with httpx.Client(timeout=60.0) as client:
        resp = client.post(url, params=params, headers=headers, json=payload)
        if resp.status_code != 200:
            # Surface the JSON error body when we have one; ElevenLabs error
            # messages are usually informative (bad voice id, bad lang code,
            # quota exhausted, etc.).
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            print(
                f"  ✗ ElevenLabs error {resp.status_code}: {detail}",
                file=sys.stderr,
            )
            raise RuntimeError(f"ElevenLabs returned HTTP {resp.status_code}")
        audio_bytes = resp.content

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(actual_rate)
        wf.writeframes(audio_bytes)

    print(f"  ✓ {output_path} ({len(audio_bytes)} bytes)")


def list_languages_and_voices() -> None:
    """Print all supported languages and voice characters."""
    from voxtera.lang_config import (
        chirp3_voice_characters,
        google_locale_for,
        language_codes,
        language_entry,
    )

    characters = chirp3_voice_characters()
    print("Available Google Chirp 3 HD voices:")
    print(f"  Characters: {', '.join(c['id'] for c in characters)}")
    print()
    print("Supported languages:")
    for code in sorted(language_codes()):
        locale = google_locale_for(code)
        if locale is None:
            continue
        entry = language_entry(code)
        name = entry.get("translation_name", code) if entry else code
        print(f"  {code:5s} → {locale:8s}  ({name})")


# Sample phrases per language for --all mode
SAMPLE_PHRASES: dict[str, str] = {
    "en": "Hello, welcome to the hotel. How can I help you today?",
    "tr": "Merhaba, otele hoş geldiniz. Bugün size nasıl yardımcı olabilirim?",
    "fr": "Bonjour, bienvenue à l'hôtel. Comment puis-je vous aider?",
    "de": "Hallo, willkommen im Hotel. Wie kann ich Ihnen helfen?",
    "es": "Hola, bienvenido al hotel. ¿En qué puedo ayudarle?",
    "it": "Ciao, benvenuto in hotel. Come posso aiutarla?",
    "pt": "Olá, bem-vindo ao hotel. Como posso ajudá-lo?",
    "nl": "Hallo, welkom in het hotel. Hoe kan ik u helpen?",
    "ro": "Bună ziua, bine ați venit la hotel. Cum vă pot ajuta?",
    "ru": "Здравствуйте, добро пожаловать в отель. Чем могу помочь?",
    "ar": "مرحباً، أهلاً وسهلاً بكم في الفندق. كيف يمكنني مساعدتكم؟",
    "ja": "こんにちは、ホテルへようこそ。本日はどのようにお手伝いできますか？",
    "zh": "您好，欢迎来到酒店。今天我能为您做些什么？",
    "ko": "안녕하세요, 호텔에 오신 것을 환영합니다. 무엇을 도와드릴까요?",
    "el": "Γεια σας, καλώς ήρθατε στο ξενοδοχείο. Πώς μπορώ να σας βοηθήσω;",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate WAV files from text using Google Chirp 3 HD (default) "
            "or ElevenLabs Flash v2.5 (--provider elevenlabs)."
        )
    )
    parser.add_argument("--text", "-t", help="Text to synthesize")
    parser.add_argument(
        "--lang",
        "-l",
        default="en",
        help="Language code (e.g. en, tr, fr, de). Default: en",
    )
    parser.add_argument(
        "--provider",
        "-p",
        choices=("google", "elevenlabs"),
        default="google",
        help=(
            "TTS provider. 'google' uses Chirp 3 HD (default); 'elevenlabs' uses "
            "the demo bot's voice (Rachel by default, override via --voice-id or "
            "ELEVENLABS_VOICE_ID env var)."
        ),
    )
    parser.add_argument(
        "--voice",
        "-v",
        default="Charon",
        help=(
            "Chirp 3 HD voice character "
            "(Charon, Aoede, Kore, Leda, Orus, Puck, Zephyr). "
            "Default: Charon. Only applies when --provider google."
        ),
    )
    parser.add_argument(
        "--voice-id",
        dest="voice_id",
        help=(
            "ElevenLabs voice ID (only used when --provider elevenlabs). "
            "Defaults to ELEVENLABS_VOICE_ID from .env, or Rachel "
            f"({_DEFAULT_ELEVENLABS_VOICE_ID}) if unset."
        ),
    )
    parser.add_argument(
        "--output", "-o", help="Output WAV path (default: output/<lang>_<voice>.wav)"
    )
    parser.add_argument(
        "--translate",
        action="store_true",
        help="Translate --text from English to --lang before synthesis",
    )
    parser.add_argument(
        "--batch",
        "-b",
        help="Path to a JSON file with sentences to synthesize",
    )
    parser.add_argument(
        "--all", action="store_true", help="Generate samples for all supported languages"
    )
    parser.add_argument("--list", action="store_true", help="List available languages and voices")
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=24000,
        help="Audio sample rate in Hz (default: 24000)",
    )

    args = parser.parse_args()

    if args.list:
        list_languages_and_voices()
        return

    if not args.all and not args.text and not args.batch:
        parser.error("Either --text, --batch, or --all is required")

    # Resolve provider credentials once up front so misconfig fails fast.
    credentials_path: Path | None = None
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_model: str | None = None
    if args.provider == "google":
        credentials_path = get_google_credentials_path()
    else:  # elevenlabs
        elevenlabs_api_key, elevenlabs_voice_id, elevenlabs_model = get_elevenlabs_config(
            args.voice_id
        )

    from voxtera.lang_config import google_locale_for, language_codes

    def _synthesize_one(
        text: str,
        lang_code: str,
        out_path: Path,
        per_entry_voice: str | None = None,
        per_entry_voice_id: str | None = None,
    ) -> None:
        """Provider-aware synthesis used by single / --all / --batch paths."""
        if args.provider == "google":
            locale = google_locale_for(lang_code)
            if locale is None:
                raise RuntimeError(f"language '{lang_code}' not supported by Google TTS")
            character = per_entry_voice or args.voice
            voice_name = f"{locale}-Chirp3-HD-{character}"
            assert credentials_path is not None
            synthesize_to_wav(
                text, locale, voice_name, out_path, credentials_path, args.sample_rate
            )
        else:
            assert elevenlabs_api_key and elevenlabs_model
            voice_id = per_entry_voice_id or elevenlabs_voice_id or _DEFAULT_ELEVENLABS_VOICE_ID
            synthesize_elevenlabs_to_wav(
                text,
                lang_code,
                voice_id,
                elevenlabs_model,
                elevenlabs_api_key,
                out_path,
                args.sample_rate,
            )

    def _default_voice_tag() -> str:
        """Filename voice tag (matches default filename naming, per provider)."""
        if args.provider == "google":
            return args.voice.lower()
        # Short, stable suffix for ElevenLabs: provider name + first 6 chars of voice id.
        vid = elevenlabs_voice_id or _DEFAULT_ELEVENLABS_VOICE_ID
        return f"elevenlabs_{vid[:6]}"

    if args.batch:
        batch_path = Path(args.batch)
        if not batch_path.exists():
            print(f"ERROR: batch file not found: {batch_path}", file=sys.stderr)
            sys.exit(1)
        sentences = json.loads(batch_path.read_text(encoding="utf-8"))
        output_dir = Path(args.output) if args.output else Path("output/batch")
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Batch ({args.provider}): {len(sentences)} sentences → {output_dir}/\n")

        for i, entry in enumerate(sentences, start=1):
            text = entry["text"]
            lang = entry.get("lang", "en")
            # Per-entry voice overrides — entry["voice"] for Google character,
            # entry["voice_id"] for an explicit ElevenLabs voice ID.
            per_voice = entry.get("voice")
            per_voice_id = entry.get("voice_id")
            do_translate = entry.get("translate", False)

            if do_translate and lang != "en":
                text = translate_text(text, lang)
                print(f"  #{i} translated → {text}")

            slug = _slugify(entry["text"])
            filename = f"{i:03d}_{lang}_{slug}.wav"
            out_path = output_dir / filename
            try:
                _synthesize_one(text, lang, out_path, per_voice, per_voice_id)
            except Exception as e:
                print(f"  ✗ #{i}: {e}", file=sys.stderr)

        print(f"\nDone. {len(sentences)} files in {output_dir}/")
        return

    if args.all:
        output_dir = Path("output/tts_samples")
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Generating samples ({args.provider}) in {output_dir}/\n")

        voice_tag = _default_voice_tag()
        for code in sorted(language_codes()):
            # Skip languages Google can't handle when using Google; ElevenLabs
            # accepts any ISO 639-1 code, so we still emit even if the Google
            # locale lookup would have failed.
            if args.provider == "google" and google_locale_for(code) is None:
                continue
            text = SAMPLE_PHRASES.get(code)
            if text is None:
                print(f"  ⏭ {code}: no sample phrase defined, skipping")
                continue
            out_path = output_dir / f"{code}_{voice_tag}.wav"
            try:
                _synthesize_one(text, code, out_path)
            except Exception as e:
                print(f"  ✗ {code}: {e}", file=sys.stderr)
    else:
        if args.output:
            out_path = Path(args.output)
        else:
            out_path = Path(f"output/{args.lang}_{_default_voice_tag()}.wav")

        text = args.text
        if args.translate and args.lang != "en":
            print(f"Translating to {args.lang}...")
            text = translate_text(text, args.lang)
            print(f"  → {text}")

        if args.provider == "google":
            # Surface the resolved locale + voice for debuggability, same as before.
            locale = google_locale_for(args.lang)
            if locale is None:
                print(
                    f"ERROR: language '{args.lang}' is not supported for Google TTS.",
                    file=sys.stderr,
                )
                print("Run with --list to see supported languages.", file=sys.stderr)
                sys.exit(1)
            voice_name = f"{locale}-Chirp3-HD-{args.voice}"
            print(f"Synthesizing: lang={args.lang}, locale={locale}, voice={voice_name}")
        else:
            print(
                f"Synthesizing: provider=elevenlabs, lang={args.lang}, "
                f"voice_id={elevenlabs_voice_id}, model={elevenlabs_model}"
            )

        _synthesize_one(text, args.lang, out_path)
        print("Done.")


if __name__ == "__main__":
    main()
