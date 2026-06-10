# Voxtera — Architecture Improvement Plan

Date: 2026-06-09 · Based on a full code review of `feat/travel-voice` (HEAD `b9ac408`).

Guiding principle: nothing here blocks the Turkish demo. Items are ordered by
(risk to pilots) × (cost to fix). Each item says **what** is wrong, **why** it
matters, and **how** to fix it concretely in this codebase.

---

## P0 — Fix before real pilot traffic

### 1. Per-call recording/trace state (kill the process-global singletons)

**What.** `voxtera/call_record.py` and the trace layer use process-global
singletons keyed by `os.environ["VOXTERA_SESSION_ID"]`. The Daily path is safe
("one call = one subprocess") but the WhatsApp service is one-process-many-calls:
two simultaneous callers mix WAVs, transcripts, and trace session ids. The
caveat is documented in `whatsapp/call_bot.py` — it stops being acceptable the
day a second guest calls during a pilot.

**How.**
- Introduce a `CallContext` dataclass (session_id, hotel_id, channel, paths)
  created in `run_call_bot()` / `bot.run_bot()` and passed explicitly to
  `RawInputRecorder`, `CallAudioRecorder`, `TranscriptStageTimer`,
  `PipelineTracer`, and `TraceForwarder` constructors.
- Replace module-level state in `call_record.py` with an instance held by the
  context; keep a thin module-level facade delegating to a contextvar
  (`contextvars.ContextVar[CallContext]`) so the Daily single-process path
  needs no changes.
- Drop the `os.environ["VOXTERA_SESSION_ID"] = ...` mutation in
  `_init_call_record()` — it is the main cross-call contamination vector.
- Acceptance test: two concurrent fake calls (see item 5 harness) produce two
  clean `logs/calls/<sid>/` folders with no cross-talk.

Effort: ~1–2 days. No behavior change for Daily mode.

### 2. Split `demo-hotel/serve.py` (the god-process)

**What.** One ~2,000-line process built on std-lib `http.server` (threaded)
plus a bolted-on asyncio loop hosts: session launcher, admin API, chat
endpoint, WhatsApp webhook, the warm ConciergePipeline, and the trace/SSE hub.
Any slow concierge render or webhook flood degrades call setup; one crash takes
everything down; std-lib `http.server` has no backpressure, middleware, or
graceful shutdown.

**How** (incremental, keep the public URLs stable behind Caddy):
1. **Phase A — extract the concierge service.** Move the warm
   ConciergePipeline + `/api/concierge` + `/api/concierge/stream` into a small
   aiohttp app (`voxtera.concierge_service`), its own systemd unit + port.
   `TravelAgentBrain` and the chat UI already speak HTTP to it — only the URL
   changes (`VOXTERA_CONCIERGE_URL` already exists for exactly this).
2. **Phase B — move the WhatsApp webhook** into the same aiohttp app (it is
   already an aiohttp `create_app()` in `whatsapp/webhook.py`; today it's
   proxied through serve.py's threaded server — wire it directly instead).
3. **Phase C — leave serve.py** as launcher + admin + static + trace hub only,
   and port it to aiohttp when convenient. The SSE trace hub is the only part
   that genuinely benefits from rewriting (proper async fan-out vs threads).
- Caddy routes: `/api/concierge* → :8300`, `/whatsapp/* → :8300`, rest → :8080.
- Acceptance: kill the concierge service mid-call → voice bot speaks its
  error fallback and web/admin stay up.

Effort: Phase A+B ~2–3 days. Biggest single reliability win available.

---

## P1 — Do within the next month

### 3. One retrieval stack, fewer stores

**What.** Five stores run on one droplet: SQLite (hotel RAG), Qdrant (KB
vectors), Elasticsearch (hotel-name resolution only), Redis (sessions), MySQL
(leads). Each adds memory pressure, backup surface, and failure modes; ES is
~1.5 GB of JVM for what is essentially fuzzy name lookup over ~1,500 hotels.

**How.**
- Fold name resolution into Qdrant: index hotel names as payload + use a
  lexical scorer in Python (rapidfuzz over the ≤2k names is microseconds), or
  add a `text` payload index. The Turkish-analyzer behavior `HotelResolver`
  needs (brand keywords, 0.82 strong-score gate) is already implemented in
  Python on top of ES results — porting the candidate generator is the only
  work.
- Alternatively (if SQL is preferred): Postgres + pgvector replaces Qdrant,
  MySQL, and ES in one engine. Bigger migration; only worth it if a managed DB
  is on the roadmap anyway.
- Keep per-hotel SQLite RAG — it is a feature (per-droplet, zero-ops, cheap
  multi-tenancy), not debt.
- Acceptance: `tests/call_center/test_hotel_resolver.py` passes against the
  new resolver; ES container removed from the droplet.

Effort: ~3–4 days for the Qdrant route. Frees ~2 GB RAM on the droplet.

### 4. Converge the two brains

**What.** `BOT_BRAIN=hotel` (local RAG + Claude + tools) and
`BOT_BRAIN=travel_agent` (delegate to concierge) are two prompts, two
retrieval stacks, and two behavior surfaces. Drift between them is the same
class of bug the TravelAgentBrain docstring warns about — and the hotel demo
is drifting already (actions/tools only exist on the hotel brain; brief-mode
voice rendering only exists on the concierge).

**How.**
- Make the concierge the only brain. Add a `hotel_id` scope to
  `/api/concierge` (the decompose/triage contract already carries region;
  hotel scoping = same pattern) and route hotel-KB queries to the SQLite
  retriever behind the existing `SourceRouter` as a new source.
- Port `create_ticket` / `web_search` / `find_videos` / `find_reviews` tools
  into the concierge render step (they are already self-contained handlers in
  `voxtera/actions/`).
- Retire `RAGContextInjector` from the live pipeline once parity is proven;
  keep it for local CLI mode if useful.
- Migrate incrementally: run hotel-brain and concierge side-by-side on the
  same recorded queries (`scripts/eval_retrieval.py` pattern) until answer
  quality matches.

Effort: ~1 week, can be done in slices. Eliminates the largest source of
"works in chat, broken on voice" bugs.

### 5. Turn-taking regression harness (synthetic calls)

**What.** The three worst recent bugs (echo self-interrupt, late-interim
cutoff, RNNoise eating Bluetooth frames) are all turn-taking/audio bugs found
by manually phoning the bot. There is rich tooling (`tools/voice-test-lab`,
`STAGE_AUDIO_DEBUG`, `scripts/compare_stage_audio.py`) but no automated test
that exercises a full conversation.

**How.**
- Build `tests/voice/test_turn_taking.py` on Pipecat's transport-less testing
  pattern: feed pre-recorded WAVs (question + overlap-speech + silence
  segments) into the same processor list `run_call_bot` builds (factor the
  processor assembly into a `build_call_processors(settings, ...)` function so
  the test and production share it).
- Assert invariants from captured frames: bot reply frames are never followed
  by an interruption when input is its own TTS echo; a genuine overlap WAV
  *does* interrupt when `allow_interruptions=True`; N user turns produce
  exactly N concierge calls (catches ghost turns).
- Stub STT with a fake service emitting scripted interim/final transcripts at
  controlled timestamps — this reproduces the late-Gladia-interim bug
  deterministically, which a real STT cannot.
- Run in CI (`make test`); the WAV fixtures live in `tests/voice/fixtures/`.

Effort: ~2–3 days initial, then minutes per new scenario. Highest
bug-prevention ROI in the repo.

### 6. Deploy pipeline hardening

**What.** `scripts/deploy-droplet.sh` rsyncs the *working tree* (uncommitted
code ships), scp's `.env` wholesale, and restarts services with no health
gate or rollback. The PSTN/Caddy/dial-in side effects make it scary to run.

**How.**
- Deploy from git, not the working tree: `git archive HEAD | ssh ... tar -x`
  (or pull a tagged release on the droplet). Print the deployed SHA into
  `/opt/voxtera/app/VERSION` and expose it on `/api/admin/health`.
- Add a post-restart health gate: poll `GET /health` on each service for 30 s;
  on failure, restore the previous release dir (keep N=2 releases,
  `current` symlink — the classic capistrano layout is 20 lines of bash).
- Split secrets from tunables: `.env` stays manual/rare; a separate
  `voxtera.tunables.env` ships with deploys.
- CI: run `make lint test` on push (GitHub Actions) so the droplet never
  receives code that fails the suite.

Effort: ~1–2 days.

---

## P2 — Worth doing, not urgent

### 7. Typed, validated configuration

~50 env vars are read ad-hoc across `config.py`, module-level `os.environ`
calls (`call_bot.py`, `trace.py`, …) and YAML hotel files. Move everything
into the existing `Settings` dataclass (pydantic-settings or plain dataclass +
one `validate()`), fail fast at startup with a clear message, and log the
effective non-secret config on boot. The scattered `os.environ.get` calls in
`whatsapp/` are the first candidates.

### 8. Gate the parallel STT/TTS branch matrix behind a flag

In Daily mode every credentialed provider gets a live branch (websockets,
sessions, memory) even though only one is active. Great for the demo
dashboard; wasteful and slower to start for client deployments. Add
`PROVIDER_MATRIX_ENABLED=false` that builds only the configured provider
(the single-provider path already exists for local mode — reuse it).

### 9. Structured product telemetry

The trace bus is excellent for debugging but ephemeral. Derive a small
per-call summary event (duration, turns, languages, interruptions, latencies
p50/p95, path mix, token cost) from `record.json` at finalize time and append
to one JSONL/SQLite table. This is the data needed for the pilot report and
the CRM webhook (roadmap item 5) — generating it at the source is ~50 lines.

### 10. Security tightening before external pilots

- Admin API: single static `X-Admin-Token` → at minimum per-user tokens +
  rate limiting at Caddy; the admin surface can eject live calls.
- `.env` on the droplet is world-of-one today; move to `chmod 600` +
  `EnvironmentFile=` in systemd units (avoids leaking via process listing).
- Rotate the WhatsApp access token and keep it out of any committed file
  (verify `git log -p` history if unsure).
- Webhook endpoints already verify HMAC (good); add payload size limits and
  reject non-JSON early in `handle_webhook`.

### 11. Documentation debt

`docs/` has 30+ overlapping plan/handoff files. Promote three living docs —
`architecture.md` (point it at `data-flow-diagram.md`), `runbook.md` (deploy,
restart, logs, common incidents), `decisions/` (keep ADR style) — and move the
rest into `docs/archive/`. Future handoffs (and future Claude sessions) get
dramatically cheaper.

---

## Sequencing at a glance

| Order | Item | Effort | Unlocks |
|-------|------|--------|---------|
| 1 | P0.1 per-call state | 1–2 d | >1 concurrent WhatsApp call |
| 2 | P0.2 split serve.py (A+B) | 2–3 d | reliability, independent restarts |
| 3 | P1.5 turn-taking harness | 2–3 d | protects all voice fixes |
| 4 | P1.6 deploy hardening | 1–2 d | safe iteration speed |
| 5 | P1.3 drop ES | 3–4 d | RAM, ops surface |
| 6 | P1.4 one brain | ~1 w | ends voice/chat drift |
| 7+ | P2 items | as fits | pilot polish |

Items 1–4 together are roughly two focused weeks and convert the architecture
from "demo that survives" to "pilot that scales to a handful of hotels"
without touching what already works: Pipecat processor composition, the
concierge decision contracts, per-hotel SQLite RAG, and the observability
stack — those are the parts to keep.
