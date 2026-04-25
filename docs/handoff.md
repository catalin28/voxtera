# Manual setup handoff

The Voxtera repository scaffold is in place. This document lists everything that **you (the human) still need to do** because it touches GitHub, Atlassian, your shell, or your machine. Each step maps back to a section of the kickoff doc.

Work through this top-to-bottom; later steps assume earlier ones are done.

---

## 1. GitHub repository (kickoff §3)

### 1.1 Create the repo

1. Go to <https://github.com/new>.
2. **Owner:** `pokemonnode34-byte` (or your org if you're moving it later).
3. **Repository name:** `voxtera`.
4. **Description:** `Multilingual real-time voice agent for the tourism industry`.
5. **Visibility:** **Private**.
6. **Do NOT** check "Add a README", "Add .gitignore", or "Choose a license" — the scaffold already has them and the GitHub-generated ones would conflict.
7. Click **Create repository**.

### 1.2 Push the scaffold

Open a terminal and:

```bash
cd ~/ChatGPTPProjects/voxtera

# initialise git on the scaffold
git init -b main
git add .
git commit -m "chore: initial project scaffold (VOX-1, VOX-2, VOX-3, VOX-5, VOX-8)"

# point at your new GitHub repo
git remote add origin git@github.com:pokemonnode34-byte/voxtera.git
git push -u origin main
```

### 1.3 Configure branch protection

In the new repo on github.com:

1. **Settings → Branches → Add branch protection rule.**
2. **Branch name pattern:** `main`.
3. Enable:
   - Require a pull request before merging
   - Require approvals → **1**
   - Require status checks to pass before merging *(leave the checklist empty for now — you'll add the CI workflow as a required check after the first green CI run)*
   - Require conversation resolution before merging
   - Do not allow bypassing the above settings
4. Save.

### 1.4 Wait for first CI run, then enable required check

After the push in §1.2, GitHub Actions will run the workflow at `.github/workflows/ci.yml`. Once you see one green run on `main`:

1. **Settings → Branches → edit the `main` rule.**
2. Under **Require status checks to pass before merging**, search for `lint-and-test` and add it as required.
3. Save.

This closes VOX-1, VOX-8, and the CI half of "Definition of Done".

---

## 2. Local Python environment (kickoff §5)

### 2.1 Install `uv`

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# restart your shell or source the rc file uv tells you to
uv --version
```

### 2.2 System audio dependency

- **macOS:** `brew install portaudio`
- **Ubuntu/Debian:** `sudo apt install -y libportaudio2 portaudio19-dev`

### 2.3 Install project deps and pre-commit hooks

```bash
cd ~/ChatGPTPProjects/voxtera
make install
```

This runs `uv sync` (creates `.venv/`, installs all deps from `pyproject.toml`) and `uv run pre-commit install` (registers Git hooks).

### 2.4 Configure your `.env`

```bash
cp .env.example .env
```

Open `.env` in your editor and replace:

- `ANTHROPIC_API_KEY` — from the team password manager
- `OPENAI_API_KEY` — from the team password manager

Leave the rest at defaults.

### 2.5 Sanity-check the scaffold

```bash
make lint      # ruff + mypy should both be clean
make test      # smoke tests should pass
make run       # placeholder bot — should print "Voxtera scaffold loaded" and exit 0
```

If these all work, the scaffold half is done — VOX-2, VOX-3, VOX-5 are demonstrably complete.

---

## 3. Task tracking — GitHub Issues (replaces kickoff §6)

We chose GitHub Issues over Jira for solo-dev simplicity. **Milestones** stand in for epics (so you get progress bars per epic), and **labels** carry type + priority + sprint metadata.

`docs/jira-setup.md` is kept as a reference if you ever migrate to Jira; the live source of truth from now on is GitHub Issues.

### 3.1 One-time: install and authenticate `gh` CLI

```bash
brew install gh        # if not already installed
gh auth login          # follow the prompts, pick GitHub.com + HTTPS + browser
```

### 3.2 Run the bootstrap script

```bash
cd ~/ChatGPTPProjects/voxtera
chmod +x scripts/setup-github-issues.sh
./scripts/setup-github-issues.sh
```

This will:

- Create labels: `type:task|story|bug|chore`, `priority:highest|high|medium|low`, `sprint:1`.
- Create 9 milestones, one per epic (`VOX-E1` through `VOX-E9`).
- Create 7 Sprint 1 issues (`VOX-1`, `VOX-2`, `VOX-3`, `VOX-5`, `VOX-6`, `VOX-7`, `VOX-8`) under milestone `VOX-E1`. (`VOX-4` was Jira-only — running the script itself replaces it.)
- Auto-close `VOX-1` (repo done) and `VOX-8` (CI green) since they're already complete.

The script is **not** idempotent for issues — running it twice would create duplicates. Labels and milestones are upserted safely.

### 3.3 After running the script

Verify on GitHub:

- <https://github.com/pokemonnode34-byte/voxtera/issues> — should show 5 open issues (`VOX-2, VOX-3, VOX-5, VOX-6, VOX-7`) and 2 closed.
- <https://github.com/pokemonnode34-byte/voxtera/milestones> — 9 milestones, with VOX-E1 showing partial progress.

### 3.4 Going forward — commit + PR convention

Every commit message and PR title should reference an issue with the GitHub `#N` syntax so it auto-links:

```
git commit -m "VOX-6: add Silero VAD integration (#6)"
```

The `VOX-6:` prefix keeps a Jira-style traceable identifier; the `#6` is what GitHub uses to auto-link to the issue. To auto-close an issue when its PR merges, use `Closes #6` in the PR description.

### 3.5 Mark VOX-2, VOX-3, VOX-5 done

Once you've finished §2 of this doc (`make install`, `make test`, `make lint`, `make run` all working locally), close those three issues with a brief comment:

```bash
gh issue close 2 -c "Done — uv sync clean on local + CI."
gh issue close 3 -c "Done — make lint and make test pass locally."
gh issue close 5 -c "Done — README and .env.example committed; verified end-to-end setup."
```

That leaves **VOX-6** (the actual user story) and **VOX-7** (multilingual testing) — the next work, waiting on the architect's `bot.py`.

---

## 4. Secrets management (kickoff §7)

- Store master copies of **all** API keys in the team password manager (1Password / Bitwarden).
- Never paste keys in Slack, Jira, email, or commit messages.
- Verify before pushing: `git diff --staged | grep -Ei "sk-|api[_-]?key"` should return nothing surprising.
- The `detect-private-key` pre-commit hook adds another layer; do not bypass it.

---

## 5. Communication (kickoff §10)

Set up the channels you'll use day-to-day:

- `#voxtera-dev` — daily chat
- `#voxtera-alerts` — CI failures + production errors (later)

Cadence:

- Daily standup, 15 min (async in Slack is fine for a small team)
- Weekly sprint planning Monday, 1h
- Weekly review + retro Friday, 1h

---

## 6. After this is done

You'll be ready to start **VOX-6 — Implement local voice loop with Whisper + Claude + OpenAI TTS**. The architect will deliver `bot.py` as the next deliverable; that code will replace the placeholder at `src/voxtera/bot.py`.

If anything blocks you while working through this handoff, post in `#voxtera-dev` and tag the architect.
