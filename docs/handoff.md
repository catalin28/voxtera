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

## 3. Jira project (kickoff §6)

Open `docs/jira-setup.md` — that file has the **exact text to paste** into each epic and ticket.

### 3.1 Create the project

1. Go to <https://atlassian.com> → Jira → **Create project**.
2. **Template:** Scrum.
3. **Name:** `Voxtera`. **Key:** `VOX`. **Access:** Team-managed, private.

### 3.2 Configure issue types and workflow

- Issue types: keep the defaults (**Epic, Story, Task, Bug, Sub-task**).
- Workflow columns: `Backlog → To Do → In Progress → In Review → Done`. (`In Review` = PR open on GitHub.)

### 3.3 Create epics

In `docs/jira-setup.md`, copy the description for each epic VOX-E1 through VOX-E9 and create them with the priorities listed there.

### 3.4 Create Sprint 1 tickets

Create VOX-1 through VOX-8 from `docs/jira-setup.md`, link each to **VOX-E1**, and add them all to Sprint 1. Set the sprint goal to **"Working local voice loop in 3 languages on a developer laptop"** and the length to **1 week**.

### 3.5 Connect Jira to GitHub

1. **Jira → Project settings → Apps → install GitHub for Jira.**
2. Authorise the `pokemonnode34-byte/voxtera` repo.
3. From now on, every commit message and PR title must reference a ticket key (e.g. `VOX-6: add Silero VAD integration`).

### 3.6 Mark scaffolded tickets done

Once §1, §2, and §3 above are complete, you can move these tickets to **Done** in Jira:

- **VOX-1** — repo + branch protection
- **VOX-2** — Python project + uv
- **VOX-3** — ruff, mypy, pre-commit, Makefile
- **VOX-4** — Jira project + epics
- **VOX-5** — README + .env.example
- **VOX-8** — GitHub Actions CI (move to Done after the first green CI run)

That leaves **VOX-6** (the actual user story) and **VOX-7** (multilingual testing) for the developer to pick up next.

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
