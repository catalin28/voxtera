#!/usr/bin/env bash
# Voxtera — bootstrap GitHub Issues.
#
# Creates:
#   - 9 milestones (one per epic VOX-E1..VOX-E9)
#   - type:* and priority:* labels, plus sprint:1
#   - 7 Sprint 1 issues (VOX-1..VOX-3, VOX-5..VOX-8 — VOX-4 was Jira-only and is
#     replaced by running this script)
#
# Run once, from anywhere (it locks to the REPO variable below). Requires `gh`
# CLI authenticated as the repo owner: `gh auth login` if you haven't yet.
#
# Idempotent-ish: labels and milestones use --force / API upsert. Issues are
# only safe to run ONCE — re-running creates duplicates.

set -euo pipefail

REPO="pokemonnode34-byte/voxtera"

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI not installed. Install: brew install gh" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh not authenticated. Run: gh auth login" >&2
  exit 1
fi

echo "==> Repo: $REPO"
echo

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
echo "==> Creating labels..."

create_label() {
  local name="$1" color="$2" desc="${3:-}"
  gh label create "$name" --color "$color" ${desc:+--description "$desc"} -R "$REPO" --force >/dev/null
  echo "  - $name"
}

create_label "type:task"     "FBCA04" "Technical work that doesn't map to a user story"
create_label "type:story"    "0E8A16" "User-facing feature"
create_label "type:bug"      "D73A4A" "Defect"
create_label "type:chore"    "C5DEF5" "Tooling, docs, refactor"
create_label "priority:highest" "B60205"
create_label "priority:high"    "D93F0B"
create_label "priority:medium"  "FBCA04"
create_label "priority:low"     "0E8A16"
create_label "sprint:1"      "5319E7" "Sprint 1: working local voice loop in 3 languages"

echo

# ---------------------------------------------------------------------------
# Milestones (epics)
# ---------------------------------------------------------------------------
echo "==> Creating milestones (epics)..."

create_milestone() {
  local title="$1" description="$2"
  # gh has no top-level milestone command; use the API. Create-or-skip-if-exists.
  if gh api "repos/$REPO/milestones" --jq '.[].title' | grep -Fxq "$title"; then
    echo "  = $title (exists, skipping)"
    return
  fi
  gh api "repos/$REPO/milestones" \
    --method POST \
    -f title="$title" \
    -f description="$description" >/dev/null
  echo "  + $title"
}

create_milestone "VOX-E1: Local Voice Loop (laptop demo)" \
  "Get a working voice agent running on a developer's laptop using OpenAI Whisper (STT) -> Claude (LLM) -> OpenAI TTS, with Silero VAD for turn-taking and interruption. Validates the core pipeline before networked or production work."

create_milestone "VOX-E2: WebRTC Transport via Daily.co" \
  "Replace LocalAudioTransport with Pipecat's Daily.co WebRTC transport so the bot can be reached from browsers and mobile clients."

create_milestone "VOX-E3: Google Chirp 3 HD TTS Integration" \
  "Replace OpenAI TTS (the Sprint-1 placeholder) with Google Chirp 3 HD for higher-quality, broader-language production voices."

create_milestone "VOX-E4: Dynamic TTS Voice Switching by Language" \
  "Pick a different Chirp 3 HD voice per detected language so each language sounds natural. Depends on VOX-E3."

create_milestone "VOX-E5: RAG Layer for Tourism Knowledge" \
  "Add a retrieval layer over a curated tourism knowledge base (hotels, attractions, transport, events) so Claude answers with grounded, up-to-date facts."

create_milestone "VOX-E6: DigitalOcean Deployment" \
  "Containerise the bot, deploy to DigitalOcean behind Nginx, and document deploy/rollback in docs/runbook.md."

create_milestone "VOX-E7: Twilio Phone Integration" \
  "Expose the bot over a phone number via Twilio Voice so users can call in."

create_milestone "VOX-E8: Admin Dashboard" \
  "Internal dashboard for usage, latency, language distribution, and per-conversation transcripts."

create_milestone "VOX-E9: CRM Webhook Integration" \
  "Push qualifying conversations (lead intent, booking inquiries) into the customer's CRM via webhook."

echo

# ---------------------------------------------------------------------------
# Issues (Sprint 1, all under VOX-E1)
# ---------------------------------------------------------------------------
echo "==> Creating Sprint 1 issues..."

E1="VOX-E1: Local Voice Loop (laptop demo)"

create_issue() {
  local title="$1" milestone="$2" body="$3"
  shift 3
  local labels="$*"
  echo "  + $title"
  gh issue create -R "$REPO" \
    --title "$title" \
    --body "$body" \
    --milestone "$milestone" \
    --label "$labels" >/dev/null
}

create_issue "VOX-1: Create GitHub repo + branch protection" "$E1" \
'## Description
Create the private `voxtera` GitHub repo under the `pokemonnode34-byte` account, initialised with README, MIT license, and Python `.gitignore`. Configure branch protection on `main` if desired.

## Acceptance criteria
- [x] Repo `pokemonnode34-byte/voxtera` exists and is private.
- [x] Lead developer can clone the repo via SSH.
- [ ] (Optional, solo dev) Branch protection rule on `main`.

## Notes
For a solo developer, branch protection with required approvals is a footgun (no one to approve your own PRs). Consider a minimal ruleset that only blocks force-pushes and deletions, or skip entirely until the team grows.' \
  "type:task,priority:highest,sprint:1"

create_issue "VOX-2: Set up Python project with uv + pyproject.toml" "$E1" \
'## Description
Use `uv` to scaffold the Python project. Add `pipecat-ai[anthropic,openai,silero,local]>=0.0.85`, `loguru`, `python-dotenv` as runtime deps, and `pytest`, `ruff`, `mypy`, `pre-commit` as dev deps. Pin Python to 3.12 via `.python-version`.

## Acceptance criteria
- [ ] `uv sync` succeeds on a fresh clone.
- [ ] `uv run python -c "import voxtera"` works.
- [ ] `uv.lock` is committed.' \
  "type:task,priority:highest,sprint:1"

create_issue "VOX-3: Configure ruff, mypy, pre-commit, Makefile" "$E1" \
'## Description
Add `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]` blocks to `pyproject.toml`. Add `.pre-commit-config.yaml` with ruff + standard hygiene hooks + secret detection. Add a `Makefile` with `install`, `run`, `test`, `lint`, `format`, `clean` targets.

## Acceptance criteria
- [ ] `make lint` runs ruff and mypy.
- [ ] `make test` runs pytest with all 4 smoke tests passing.
- [ ] `pre-commit run --all-files` passes on a clean checkout.' \
  "type:task,priority:high,sprint:1"

create_issue "VOX-5: Write README + .env.example" "$E1" \
'## Description
Write `README.md` covering prerequisites, setup, run, test, and links to docs. Write `.env.example` with `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LOG_LEVEL`, `BOT_NAME`, `DEFAULT_TTS_VOICE`, `VAD_STOP_SECS`. Confirm `.env` is in `.gitignore`.

## Acceptance criteria
- [ ] A new developer can follow the README from clone to `make run` without asking questions.
- [ ] `.env.example` is committed; `.env` is not.' \
  "type:task,priority:high,sprint:1"

create_issue "VOX-6: Implement local voice loop with Whisper + Claude + OpenAI TTS" "$E1" \
'## Description
Build the local voice loop. See [`docs/user-stories.md`](../blob/main/docs/user-stories.md) for the full user story. Pipeline: `LocalAudioTransport` -> Silero VAD -> Whisper STT -> Claude LLM -> OpenAI TTS -> `LocalAudioTransport`. System prompt enforces same-language replies. Loguru logs detected language per turn.

The architect will deliver `bot.py` as the next deliverable; this issue tracks integrating it and proving it works end-to-end.

## Acceptance criteria
- [ ] Live demo works in at least 3 languages (e.g. English, French, Japanese).
- [ ] Logs show the detected language for each user turn.
- [ ] Interruption works (user can talk over the bot).
- [ ] End-to-end latency under ~3s on a normal laptop.
- [ ] No silent crashes on errors.

## Risks
- Mic permissions on macOS may need granting Terminal access in System Settings.
- Language drift: Claude may default to English mid-conversation if the system prompt is not strict enough.' \
  "type:story,priority:highest,sprint:1"

create_issue "VOX-7: Test local voice loop in 3+ languages and document results" "$E1" \
'## Description
Run live conversations in at least three languages (English, French, Japanese minimum). Record observed latency, language-detection accuracy, and any drift. Add findings to `docs/user-stories.md` under a new "VOX-7 results" section.

## Acceptance criteria
- [ ] Each language has at least one transcript captured.
- [ ] Observed latency vs. acceptance criterion (~3s) is documented.
- [ ] Drift / edge cases are filed as separate bug issues.

## Blocked by
VOX-6.' \
  "type:task,priority:high,sprint:1"

create_issue "VOX-8: Set up GitHub Actions CI (lint + test)" "$E1" \
'## Description
Add `.github/workflows/ci.yml` running ruff, mypy, and pytest on push and PR to `main`. Install `portaudio19-dev` before `uv sync` so `pyaudio` builds.

## Acceptance criteria
- [x] First push to `main` shows a green CI run.
- [ ] (Optional, solo dev) Branch protection requires CI green before merging.' \
  "type:task,priority:high,sprint:1"

echo
echo "==> Closing already-done issues..."

# Find and close VOX-1 (repo done) and VOX-8 (CI green).
close_issue() {
  local title_match="$1" comment="$2"
  local num
  num=$(gh issue list -R "$REPO" --search "$title_match in:title" --json number,title --jq '.[0].number' || true)
  if [[ -n "$num" && "$num" != "null" ]]; then
    gh issue close "$num" -R "$REPO" --comment "$comment" >/dev/null
    echo "  - closed #$num"
  fi
}

close_issue "VOX-1: Create GitHub repo" \
  "Done. Repo created and initial scaffold pushed to main."
close_issue "VOX-8: Set up GitHub Actions CI" \
  "Done. CI #3 green on main: ruff, mypy, and pytest all pass after adding portaudio19-dev install."

echo
echo "==> Done. Open: https://github.com/$REPO/issues"
