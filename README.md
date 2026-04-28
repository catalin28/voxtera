# Voxtera

Multilingual real-time voice agent for the tourism industry. Voxtera detects the user's language automatically and responds in that same language, handling tourism queries (hotels, attractions, transport, dining, safety, cultural tips, local events).

## Status

Early development. Sprint 1 goal: a working local voice loop on a developer laptop using OpenAI Whisper (STT) + Claude (LLM) + OpenAI TTS — see [`docs/user-stories.md`](docs/user-stories.md) for the first user story (`VOX-6`).

## Prerequisites

- macOS, Linux, or Windows (WSL2 recommended on Windows)
- Python 3.12+ (`python3 --version`)
- Git
- Working microphone and speakers
- An OpenAI API key (for Whisper STT and TTS)
- An Anthropic API key (for Claude LLM)
- [`uv`](https://docs.astral.sh/uv/) — fast Python package manager
- On macOS: `brew install portaudio`
- On Ubuntu/Debian: `sudo apt install libportaudio2`

Install `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Setup

```bash
# Clone
git clone git@github.com:pokemonnode34-byte/voxtera.git
cd voxtera

# Install deps + pre-commit hooks
make install

# Configure environment
cp .env.example .env
# then edit .env and fill in OPENAI_API_KEY and ANTHROPIC_API_KEY
```

## Run

```bash
make run
```

Speak into your microphone — the bot transcribes via Whisper, replies via Claude, and speaks the reply back via OpenAI TTS in the same language you used.

### Input modes

The bot supports three ways to provide input. Configure with `INPUT_MODE` in `.env` or as a one-off env var:

| Mode | Behaviour | When to use |
|---|---|---|
| `voice` | Microphone only (the original) | Default voice-agent experience. |
| `text` | Keyboard only (mic disabled, no Silero model loaded) | Libraries, trains, quiet spaces — type your question, hear the reply through headphones. |
| `hybrid` *(default)* | Both — speak or type, mix per turn | Most flexible. Active by default. |

Examples:

```bash
make run                              # uses INPUT_MODE from .env (default: hybrid)
INPUT_MODE=text make run              # one-off keyboard-only run
INPUT_MODE=voice make run             # one-off mic-only run
RNNOISE_ENABLED=true make run         # one-off mic denoising for demo/noisy rooms
INPUT_MODE=hybrid RNNOISE_ENABLED=true make run
```

In text or hybrid mode, type a question and press Enter. Type `quit`, `exit`, or `bye` to end the session cleanly. The bot's reply always plays through your speakers/headphones regardless of mode.

### RNNoise (demo denoiser)

Set `RNNOISE_ENABLED=true` to denoise microphone input before VAD and STT. This helps with fan/ambient noise during demos while preserving interruption behavior.

## Test & lint

```bash
make test     # run pytest
make lint     # ruff + mypy
make format   # auto-fix formatting
```

## Repository layout

```
voxtera/
├── .github/workflows/ci.yml      # GitHub Actions: lint + test
├── docs/                         # architecture, user stories, ADRs
├── src/voxtera/                  # application code
├── tests/                        # pytest tests
├── .env.example                  # template for .env (do not commit real .env)
├── pyproject.toml                # deps and tool config
├── Makefile                      # common commands
└── README.md
```

## Documentation

- [Architecture overview](docs/architecture.md)
- [User stories](docs/user-stories.md)
- [Setup deep-dive](docs/setup.md)
- [RAG architecture (VOX-E5)](docs/rag-architecture.md)
- [RAG implementation plan (LLM coder hand-off)](docs/rag-implementation-plan.md)
- [Architecture Decision Records](docs/decisions/)

## Project management

- Tracked in GitHub Issues. Milestones stand in for epics (`VOX-E1`..`VOX-E9`); labels carry type + priority + sprint.
- Every commit and PR title should reference an issue, e.g. `VOX-6: add Silero VAD integration (#6)`. Use `Closes #6` in PR descriptions to auto-close when merged.
- See [`docs/handoff.md`](docs/handoff.md) for setup steps (push, CI, GitHub Issues bootstrap, local install).
- Bootstrap script: [`scripts/setup-github-issues.sh`](scripts/setup-github-issues.sh).

## License

MIT — see [`LICENSE`](LICENSE).
