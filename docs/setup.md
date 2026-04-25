# Voxtera local setup — deep dive

A longer companion to the README. Use this when something goes wrong with `make install` or `make run`, or when onboarding a new developer.

## 1. System prerequisites

### macOS

```bash
xcode-select --install        # if you don't already have it
brew install python@3.12 git portaudio
```

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git libportaudio2 portaudio19-dev
```

### Windows

Use **WSL2** with an Ubuntu image and follow the Ubuntu instructions.

## 2. Install `uv`

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# then restart your shell or `source ~/.bashrc`
uv --version
```

## 3. Clone and install

```bash
git clone git@github.com:pokemonnode34-byte/voxtera.git
cd voxtera
make install
```

`make install` runs:

- `uv sync` — creates `.venv/` and installs all deps from `pyproject.toml`
- `uv run pre-commit install` — registers pre-commit hooks for lint + secret detection

## 4. Configure environment

```bash
cp .env.example .env
```

Then open `.env` in your editor and replace:

- `ANTHROPIC_API_KEY` with your real key from the team password manager
- `OPENAI_API_KEY` with your real key from the team password manager

Leave the other variables at their defaults until you have a reason to change them.

## 5. Verify

```bash
make test       # smoke tests should pass
make lint       # ruff and mypy should be clean
make run        # placeholder run — will become the live voice loop in VOX-6
```

## 6. Common problems

### "Could not find PortAudio"

You missed the system PortAudio install. Re-run the system prerequisites step for your OS.

### macOS — "no microphone detected"

Open **System Settings → Privacy & Security → Microphone** and grant access to your terminal app (Terminal.app, iTerm, VS Code, etc.). You may need to fully quit and reopen the terminal.

### "uv: command not found"

The `uv` installer adds itself to `~/.local/bin`. Make sure that's on your `PATH`, or restart your shell.

### Pre-commit blocks a commit

Run `make format` to auto-fix lint/format issues, then re-stage and commit again. If the `detect-private-key` hook fires, it's a real problem — you have a key in your changes; remove it before committing.
