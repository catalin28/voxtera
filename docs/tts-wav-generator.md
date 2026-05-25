uv run python tools/voice-test-lab/voice_test_lab.py# TTS WAV Generator

Generate WAV audio files from text in multiple languages.

Two options are available: a **desktop GUI** (Voice Test Lab) for interactive use, and a **CLI script** for batch/automated generation.

---

## Desktop UI — Voice Test Lab

A tkinter desktop application for authoring test scripts, generating voice clips, and rehearsing them against the Voxtera bot.

### Run

```bash
python3 tools/voice-test-lab/voice_test_lab.py
```

### Prerequisites

- `edge-tts` installed:
  ```bash
  uv pip install edge-tts
  ```
- `tkinter` available (ships with python.org Python; for Homebrew Python: `brew install python-tk@3.12`)
- `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in `.env` (for automatic translation)

### Features

1. **Add questions** — type text in English, pick a target language and voice, optionally auto-translate.
2. **Generate WAV clips** — synthesize the entire script to WAV files in one click.
3. **Rehearse** — step through clips sequentially, playing each into the bot's mic (via BlackHole on macOS).

### Output

Generated WAV clips are saved to `tools/voice-test-lab/clips/`. Test scripts (JSON) are saved to `tools/voice-test-lab/scripts/` for reuse.

### Supported Languages

The UI supports a wide range of locales including English (US/UK/AU), Turkish, French, German, Spanish, Italian, Romanian, and more — powered by Microsoft Edge TTS voices.

---

## CLI Script — `generate_tts_wav.py`

Generate WAV files from the command line using Google Chirp 3 HD.

### Prerequisites

1. **Google Cloud credentials** — a service account JSON with the Cloud Text-to-Speech API enabled.
2. Set `GOOGLE_APPLICATION_CREDENTIALS` in your `.env` file or export it:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json
   ```
3. Dependencies installed (`uv sync`).

## Quick Start

```bash
uv run python scripts/generate_tts_wav.py --text "Hello, welcome" --lang en
```

Output: `output/en_charon.wav`

## Usage

```
uv run python scripts/generate_tts_wav.py [OPTIONS]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--text` | `-t` | Text to synthesize (required unless `--all`) |
| `--lang` | `-l` | Language code (default: `en`) |
| `--voice` | `-v` | Voice character (default: `Charon`) |
| `--translate` | | Translate `--text` from English to `--lang` before synthesis |
| `--output` | `-o` | Custom output WAV path |
| `--all` | | Generate samples for all supported languages |
| `--list` | | List available languages and voices |
| `--sample-rate` | | Audio sample rate in Hz (default: 24000) |

## Examples

### Single language

```bash
# English
uv run python scripts/generate_tts_wav.py -t "Hello, how can I help you?" -l en

# Turkish
uv run python scripts/generate_tts_wav.py -t "Merhaba, size nasıl yardımcı olabilirim?" -l tr

# French with a female voice
uv run python scripts/generate_tts_wav.py -t "Bonjour, comment puis-je vous aider?" -l fr -v Aoede
```

### Auto-translate from English

Write your text in English and let the tool translate it before generating the WAV. Requires `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in `.env`.

```bash
# English → Turkish
uv run python scripts/generate_tts_wav.py -t "What can I eat today?" -l tr --translate

# English → French with a female voice
uv run python scripts/generate_tts_wav.py -t "Hello, welcome to the hotel" -l fr --translate -v Aoede

# English → German
uv run python scripts/generate_tts_wav.py -t "Do you have a pool here?" -l de --translate
```

The tool prints the translated text before synthesizing so you can verify it.

### Generate all languages

Produces one WAV per supported language using built-in sample phrases:

```bash
uv run python scripts/generate_tts_wav.py --all
```

Output goes to `output/tts_samples/` (e.g. `en_charon.wav`, `tr_charon.wav`, `fr_charon.wav`).

### Custom output path

```bash
uv run python scripts/generate_tts_wav.py -t "Hola" -l es -o recordings/spanish.wav
```

### List supported languages and voices

```bash
uv run python scripts/generate_tts_wav.py --list
```

## Available Voices

All voices are Google Chirp 3 HD characters. The same character works across all supported languages.

| Character | Description |
|-----------|-------------|
| Charon | Deep male (default) |
| Aoede | Warm female |
| Kore | Gentle female |
| Leda | Cheerful female |
| Orus | Formal male |
| Puck | Energetic male |
| Zephyr | Calm male |

## Output Format

- **Format:** WAV (PCM)
- **Sample rate:** 24,000 Hz (configurable via `--sample-rate`)
- **Channels:** Mono
- **Bit depth:** 16-bit

## Supported Languages

Run `--list` for the full current set. Common ones include:

| Code | Language |
|------|----------|
| en | English |
| tr | Turkish |
| fr | French |
| de | German |
| es | Spanish |
| it | Italian |
| pt | Portuguese |
| nl | Dutch |
| ro | Romanian |
| ru | Russian |
| ar | Arabic |
| ja | Japanese |
| zh | Chinese |
| ko | Korean |
| el | Greek |

## Troubleshooting

| Error | Fix |
|-------|-----|
| `GOOGLE_APPLICATION_CREDENTIALS not set` | Add it to `.env` or export in shell |
| `credentials file not found` | Check the path is correct and the file exists |
| `language 'xx' is not supported` | Run `--list` to see valid codes |
| `google.api_core` permission error | Ensure Cloud Text-to-Speech API is enabled in your GCP project |
