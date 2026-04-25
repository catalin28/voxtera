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
- [Architecture Decision Records](docs/decisions/)

## Project management

- Jira project: `VOX` (epics `VOX-E1`..`VOX-E9`, tickets `VOX-1`..)
- Every commit and PR title must reference a Jira key, e.g. `VOX-6: add Silero VAD integration`
- See [`docs/handoff.md`](docs/handoff.md) for the manual setup steps (GitHub repo creation, branch protection, Jira project, CI, etc.)

## License

MIT — see [`LICENSE`](LICENSE).
