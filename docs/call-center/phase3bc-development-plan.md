# Phase 3bc — Development Plan

**Branch:** `feat/VOX-concierge-ui-timings`
**Author:** GitHub Copilot (autonomous)

---

## 1. Design decisions

### 1.1 Where the UI lives

The existing `/call_center/` admin UI (`src/voxtera/call_center/ui.html`)
is a dark Twitter-blue developer dashboard intended for ES / Qdrant
operations. The concierge is a **guest-facing** surface — restyling
the admin panel inline would conflate two very different audiences.

Decision: the concierge UI is a **new standalone page in `demo-hotel/`**
(`voxtera-concierge.html`), sibling to `voxtera-demo.html`,
`voxtera-hotels.html`, `voxtera-cloudbeds.html`, and `voxtera.html`.
This matches the existing marketing-site layout and is served by the
same `demo-hotel/serve.py` static file handler that powers the rest of
the public pages.

### 1.2 Visual differentiation

User direction: *"the page should be in same style … the colors should
be different but not too different, the user should feel like is in the
same site but a different functionality."*

Decision: keep the demo design tokens verbatim
(`--cream`, `--ink`, `--gold`, Fraunces / Instrument Serif / JetBrains
Mono, paper-grain SVG noise) and introduce **one** new accent variable:

```css
--accent:#1f6e6a;        /* sea-green / teal */
--accent-deep:#154f4c;
--accent-soft:#2f8d88;
--accent-tint:rgba(31,110,106,0.10);
```

Teal is in the same warm/muted family as gold and rust but reads as
*places & travel* rather than *booking*. It replaces `--rust` only on
concierge-specific affordances: nav hover, nav-cta hover, mode tag,
section eyebrows, send button, input focus, hotel-card score, debug
timing chips.

### 1.3 Wiring `/api/concierge` from a static-file server

`demo-hotel/serve.py` is a threaded `SimpleHTTPRequestHandler`;
`ConciergeAgent` is async (uses `aiohttp.ClientSession`). The existing
codebase already has the pattern for bridging async work from sync
handlers (see `_rag_context` / `_handle_chat` — `asyncio.new_event_loop()
+ run_until_complete`).

Decision: add `_handle_concierge` (≤25 lines) that:
1. Reads JSON body `{utterance, region}` via existing `_read_json_body`.
2. Spins up a fresh event loop + `aiohttp.ClientSession`.
3. Calls `ConciergeAgent(session=session).answer(...)`.
4. Returns the full result dict via existing `_send_json(200, ...)`.

Errors → 500 with `{"error": str(exc)}`. Empty utterance → 400.

### 1.4 Timings instrumentation (3c)

`ConciergeAgent.answer()` already wraps each stage; Phase 3c adds
explicit `time.perf_counter()` measurement around each of the three
async calls plus the overall total.

- New helper `_ms(seconds) -> float` rounding to 1 decimal.
- `timings: dict[str, float]` populated incrementally; whichever stages
  ran are recorded — including failed stages (`decompose_ms` set even
  if decompose raised).
- Returned on **every** code path: happy, short-circuit (empty
  utterance / no region), decompose error, render error.
- `_short_circuit` takes `t_start, timings` and sets `total_ms` before
  returning.
- Happy-path log line: `concierge.answer region={!r} reqs={} reason={}
  timings={}`.

## 2. Implementation steps

1. **3c — Code:**
   - Add `import time` to `concierge.py`.
   - Rewrite `answer()` to record `decompose_ms`, `retrieve_ms`,
     `render_ms`, `total_ms`.
   - Rewrite `_short_circuit(...)` signature to take `t_start, timings`.
   - Add `_ms()` module helper.
   - All return dicts include `"timings": timings`.
2. **3c — Tests:** add `TestTimings` class to
   `tests/call_center/test_concierge.py` with two tests:
   - Happy path: all four keys present, monotonic, total ≥ sum(stages).
   - Short-circuit (empty utterance): only `total_ms` present.
3. **3b — UI:** author `demo-hotel/voxtera-concierge.html` (standalone,
   ~430 lines incl. styles + script).
4. **3b — Route:** add `_handle_concierge` to `demo-hotel/serve.py` and
   register `/api/concierge` in `do_POST`.
5. **Verify:** `pytest tests/call_center/test_concierge.py -q` →
   10 passed.
6. **Docs:** write `phase3bc-{user-story,development-plan,test-report,
   remaining-work}.md`.
7. **Commit + merge:** `--no-ff` to `develop`, push to origin.

## 3. Out of scope (deferred to follow-on phases)

| Item | Phase |
|---|---|
| Voice / call mode on the concierge page (orb, mic, TTS) | 3d |
| i18n of UI copy + region list driven from config | 3e |
| Cross-encoder re-rank | 3a (still deferred) |
| Concierge in the live voice pipeline (bot.py) | 4 |
| Per-stage spans → OpenTelemetry / Jaeger | ops backlog |
