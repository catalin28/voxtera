# Jira setup — copy-paste-ready content

This file contains the exact text to paste into Jira when creating the `VOX` project. Once the project exists, you can paste each block into the relevant epic/ticket description field.

> Project key: **VOX**
> Project name: **Voxtera**
> Template: **Scrum** (1-week sprints)
> Workflow: **Backlog → To Do → In Progress → In Review → Done**

---

## Epics

Create these nine epics first. Set their priorities as shown.

### VOX-E1 — Local Voice Loop (laptop demo)
**Priority:** Highest
**Description:**
Get a working voice agent running on a developer's laptop using OpenAI Whisper (STT) → Claude (LLM) → OpenAI TTS, with Silero VAD for turn-taking and interruption. This epic validates the core conversation pipeline and multilingual logic before any networked or production work begins.

### VOX-E2 — WebRTC Transport via Daily.co
**Priority:** High
**Description:**
Replace `LocalAudioTransport` with Pipecat's Daily.co WebRTC transport so the bot can be reached from browsers and mobile clients.

### VOX-E3 — Google Chirp 3 HD TTS Integration
**Priority:** High
**Description:**
Replace OpenAI TTS (the Sprint-1 placeholder) with Google Chirp 3 HD for higher-quality, broader-language production voices.

### VOX-E4 — Dynamic TTS Voice Switching by Language
**Priority:** High
**Description:**
Pick a different Chirp 3 HD voice per detected language so each language sounds natural. Depends on VOX-E3.

### VOX-E5 — RAG Layer for Tourism Knowledge
**Priority:** Medium
**Description:**
Add a retrieval layer over a curated tourism knowledge base (hotels, attractions, transport, events) so Claude can answer with grounded, up-to-date facts.

### VOX-E6 — DigitalOcean Deployment
**Priority:** Medium
**Description:**
Containerise the bot, deploy to DigitalOcean behind Nginx, and document the deploy/rollback process in `docs/runbook.md`.

### VOX-E7 — Twilio Phone Integration
**Priority:** Medium
**Description:**
Expose the bot over a phone number via Twilio Voice so users can call in.

### VOX-E8 — Admin Dashboard
**Priority:** Low
**Description:**
Internal dashboard for usage, latency, language distribution, and per-conversation transcripts.

### VOX-E9 — CRM Webhook Integration
**Priority:** Low
**Description:**
Push qualifying conversations (lead intent, booking inquiries) into the customer's CRM via webhook.

---

## Sprint 1 tickets (under VOX-E1)

Create these eight tickets in Sprint 1 and link each to epic **VOX-E1**.

### VOX-1 — Create GitHub repo + branch protection
- **Type:** Task
- **Estimate:** 1h
- **Description:**
  Create the private `voxtera` GitHub repo under the `pokemonnode34-byte` account, initialised with README, MIT license, and Python `.gitignore`. Configure branch protection on `main`: require PR, require 1 approval, require status checks (once CI exists), require conversation resolution, no bypass.
- **Acceptance criteria:**
  - Repo `pokemonnode34-byte/voxtera` exists and is private.
  - `main` is protected per the rules above.
  - The lead developer can clone the repo via SSH.

### VOX-2 — Set up Python project with uv + pyproject.toml
- **Type:** Task
- **Estimate:** 2h
- **Description:**
  Use `uv` to scaffold the Python project. Add `pipecat-ai[anthropic,openai,silero,local]>=0.0.85`, `loguru`, `python-dotenv` as runtime deps, and `pytest`, `ruff`, `mypy`, `pre-commit` as dev deps. Pin Python to 3.12 via `.python-version`.
- **Acceptance criteria:**
  - `uv sync` succeeds on a fresh clone.
  - `uv run python -c "import voxtera"` works.

### VOX-3 — Configure ruff, mypy, pre-commit, Makefile
- **Type:** Task
- **Estimate:** 2h
- **Description:**
  Add `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]` blocks to `pyproject.toml` per the kickoff doc §5.4. Add `.pre-commit-config.yaml` with ruff + standard hygiene hooks + secret detection. Add a `Makefile` with `install`, `run`, `test`, `lint`, `format`, `clean` targets.
- **Acceptance criteria:**
  - `make lint` runs ruff and mypy.
  - `make test` runs pytest.
  - `pre-commit run --all-files` passes on a clean checkout.

### VOX-4 — Set up Jira project + initial epics
- **Type:** Task
- **Estimate:** 1h
- **Description:**
  Create the `VOX` Jira project (Scrum template, team-managed). Configure issue types (Epic, Story, Task, Bug, Sub-task). Configure workflow columns. Create epics VOX-E1 through VOX-E9 with priorities from the kickoff doc.
- **Acceptance criteria:**
  - Project `VOX` exists and the team has access.
  - All nine epics are visible in the backlog.

### VOX-5 — Write README + .env.example
- **Type:** Task
- **Estimate:** 1h
- **Description:**
  Write `README.md` covering prerequisites, setup, run, test, and links to docs. Write `.env.example` with `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LOG_LEVEL`, `BOT_NAME`, `DEFAULT_TTS_VOICE`, `VAD_STOP_SECS`. Confirm `.env` is in `.gitignore`.
- **Acceptance criteria:**
  - A new developer can follow the README from clone to `make run` without asking questions.
  - `.env.example` is committed; `.env` is not.

### VOX-6 — Implement local voice loop with Whisper + Claude + OpenAI TTS
- **Type:** Story
- **Estimate:** 1d
- **Description:**
  See `docs/user-stories.md` for the full story. Build a Pipecat pipeline `LocalAudioTransport → Silero VAD → Whisper STT → Claude LLM → OpenAI TTS → LocalAudioTransport`. System prompt enforces same-language replies. Loguru logs detected language per turn.
- **Acceptance criteria:**
  - Live demo works in at least 3 languages (e.g. English, French, Japanese).
  - Logs show the detected language for each user turn.
  - Interruption works (user can talk over the bot).
  - End-to-end latency under ~3s on a normal laptop.
  - No silent crashes on errors.

### VOX-7 — Test local voice loop in 3+ languages and document results
- **Type:** Task
- **Estimate:** 2h
- **Description:**
  Run live conversations in at least three languages (English, French, Japanese minimum). Record observed latency, language-detection accuracy, and any drift. Add findings to `docs/user-stories.md` (section "VOX-7 results") or as a new doc.
- **Acceptance criteria:**
  - Each language has at least one transcript captured.
  - Observed latency vs. acceptance criterion (~3s) is documented.
  - Any drift or edge cases are filed as new bug tickets if appropriate.

### VOX-8 — Set up GitHub Actions CI (lint + test)
- **Type:** Task
- **Estimate:** 2h
- **Description:**
  Add `.github/workflows/ci.yml` running ruff, mypy, and pytest on push and PR to `main`. Once green on `main`, enable the "Require status checks to pass before merging" branch-protection rule and select this workflow.
- **Acceptance criteria:**
  - First push to `main` shows a green CI run.
  - Branch protection requires CI green before merging to `main`.

---

## Sprint 1 metadata

- **Goal:** "Working local voice loop in 3 languages on a developer laptop"
- **Length:** 1 week
- **All eight tickets above are in Sprint 1.**
- **Standup:** daily, 15 min, async in `#voxtera-dev` or sync video.

## Connecting Jira ↔ GitHub

After the project and repo both exist:

1. In Jira: **Project settings → Apps → install GitHub for Jira**.
2. Authorise the `pokemonnode34-byte/voxtera` repo.
3. From now on, every commit message and PR title must reference a ticket key, e.g.:
   ```
   git commit -m "VOX-6: add Silero VAD integration"
   ```
   Jira will auto-link the commit/PR to the ticket and reflect PR status on the ticket.
