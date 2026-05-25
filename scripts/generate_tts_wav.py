"""Generate WAV files from text in multiple languages using Google Chirp 3 HD.

Usage examples:
    # Single language (English)
    uv run python scripts/generate_tts_wav.py --text "Hello, welcome to the hotel" --lang en

    # Turkish (pre-translated)
    uv run python scripts/generate_tts_wav.py --text "Merhaba, otele hoş geldiniz" --lang tr

    # Auto-translate English to Turkish
    uv run python scripts/generate_tts_wav.py --text "What can I eat today?" --lang tr --translate

    # Auto-translate English to French with a specific voice character
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
    GOOGLE_APPLICATION_CREDENTIALS must point to a valid service account JSON file.
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
        description="Generate WAV files from text using Google Chirp 3 HD TTS"
    )
    parser.add_argument("--text", "-t", help="Text to synthesize")
    parser.add_argument(
        "--lang",
        "-l",
        default="en",
        help="Language code (e.g. en, tr, fr, de). Default: en",
    )
    parser.add_argument(
        "--voice",
        "-v",
        default="Charon",
        help=(
            "Chirp 3 HD voice character "
            "(Charon, Aoede, Kore, Leda, Orus, Puck, Zephyr). "
            "Default: Charon"
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

    credentials_path = get_google_credentials_path()

    from voxtera.lang_config import google_locale_for, language_codes

    if args.batch:
        batch_path = Path(args.batch)
        if not batch_path.exists():
            print(f"ERROR: batch file not found: {batch_path}", file=sys.stderr)
            sys.exit(1)
        sentences = json.loads(batch_path.read_text(encoding="utf-8"))
        output_dir = Path(args.output) if args.output else Path("output/batch")
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Batch: {len(sentences)} sentences → {output_dir}/\n")

        for i, entry in enumerate(sentences, start=1):
            text = entry["text"]
            lang = entry.get("lang", "en")
            voice = entry.get("voice", args.voice)
            do_translate = entry.get("translate", False)

            locale = google_locale_for(lang)
            if locale is None:
                print(f"  ✗ #{i}: language '{lang}' not supported, skipping")
                continue

            if do_translate and lang != "en":
                text = translate_text(text, lang)
                print(f"  #{i} translated → {text}")

            voice_name = f"{locale}-Chirp3-HD-{voice}"
            slug = _slugify(entry["text"])
            filename = f"{i:03d}_{lang}_{slug}.wav"
            out_path = output_dir / filename
            try:
                synthesize_to_wav(
                    text,
                    locale,
                    voice_name,
                    out_path,
                    credentials_path,
                    args.sample_rate,
                )
            except Exception as e:
                print(f"  ✗ #{i}: {e}", file=sys.stderr)

        print(f"\nDone. {len(sentences)} files in {output_dir}/")
        return

    if args.all:
        output_dir = Path("output/tts_samples")
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Generating samples in {output_dir}/\n")

        for code in sorted(language_codes()):
            locale = google_locale_for(code)
            if locale is None:
                continue
            text = SAMPLE_PHRASES.get(code)
            if text is None:
                # Skip languages without a sample phrase
                print(f"  ⏭ {code}: no sample phrase defined, skipping")
                continue
            voice_name = f"{locale}-Chirp3-HD-{args.voice}"
            out_path = output_dir / f"{code}_{args.voice.lower()}.wav"
            try:
                synthesize_to_wav(
                    text, locale, voice_name, out_path, credentials_path, args.sample_rate
                )
            except Exception as e:
                print(f"  ✗ {code}: {e}", file=sys.stderr)
    else:
        locale = google_locale_for(args.lang)
        if locale is None:
            print(
                f"ERROR: language '{args.lang}' is not supported for Google TTS.", file=sys.stderr
            )
            print("Run with --list to see supported languages.", file=sys.stderr)
            sys.exit(1)

        voice_name = f"{locale}-Chirp3-HD-{args.voice}"
        if args.output:
            out_path = Path(args.output)
        else:
            out_path = Path(f"output/{args.lang}_{args.voice.lower()}.wav")

        text = args.text
        if args.translate and args.lang != "en":
            print(f"Translating to {args.lang}...")
            text = translate_text(text, args.lang)
            print(f"  → {text}")

        print(f"Synthesizing: lang={args.lang}, locale={locale}, voice={voice_name}")
        synthesize_to_wav(text, locale, voice_name, out_path, credentials_path, args.sample_rate)
        print("Done.")


if __name__ == "__main__":
    main()
