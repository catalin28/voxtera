# Phase 3bc — Concierge UI + Timings — User Story

**Ticket:** VOX-RAG-P3bc-001
**Branch:** `feat/VOX-concierge-ui-timings`
**Depends on:** Phase 3 (`ConciergeAgent`)

---

## User-facing intent

> *"As a guest browsing Voxtera, I want a dedicated **Concierge** page
> in the same look and feel as the rest of the site — but visibly its
> own surface — where I can type what I'm looking for and immediately
> see which hotels match each part of the request, with the exact
> evidence the agent used and how long each pipeline stage took."*

Phase 3 shipped `ConciergeAgent` and a debug-grade JSON endpoint at
`GET /call_center/api/concierge`. Phase 3bc closes the visual + ops
loop:

- **3b — Concierge UI:** A guest-facing page (`voxtera-concierge.html`)
  in the demo-hotel cream/Fraunces aesthetic, distinguished from the
  booking demo by a teal accent. Mounted in `demo-hotel/` next to the
  other public marketing pages.
- **3c — Stage timings:** `ConciergeAgent.answer()` now records
  `decompose_ms`, `retrieve_ms`, `render_ms`, `total_ms` on the
  returned dict. Surfaced both in logs (`concierge.answer ... timings={}`)
  and inside the UI's debug drawer.

## Scope — what's in / out

| In scope | Out of scope |
|---|---|
| New static page `demo-hotel/voxtera-concierge.html` | Voice / call mode on the concierge page (Phase 3d) |
| `POST /api/concierge` proxy handler in `demo-hotel/serve.py` (sync, JSON) | Multi-turn conversation memory in UI |
| Region picker (Paris / Istanbul / Bodrum / Antalya / Rome / Barcelona) | Persisted chat history per visitor |
| Suggestion chips for common multi-requirement queries | i18n of UI copy (English only for now) |
| Hotel result cards with per-requirement evidence | Cross-encoder re-rank (Phase 3a, still deferred) |
| Collapsible debug drawer: decomposition JSON + timings chips | p95 latency claims / load-test report |
| Teal accent (`--accent:#1f6e6a`) — distinct from rust booking demo | Restyle of the dark `/call_center/` admin UI |
| `timings: dict[str, float]` on every `ConciergeAgent.answer()` return path | Per-stage tracing into spans / OpenTelemetry |
| 2 new unit tests (`TestTimings`) — total 10/10 green | Live latency baseline numbers (live smoke optional) |

## Acceptance criteria

1. Visiting `http://localhost:8000/voxtera-concierge.html` (served by
   `demo-hotel/serve.py`) renders the concierge page in cream/Fraunces
   style with the **teal** accent, marketing nav with "Concierge"
   active, and the chat thread + input bar at the bottom.
2. Typing a request and pressing Ask posts JSON to `/api/concierge`,
   which calls `ConciergeAgent.answer()` and returns the full result
   dict.
3. The assistant bubble shows: the rendered answer (Instrument Serif
   italic for warmth), a list of up to 5 hotel cards each with their
   per-requirement evidence text, and a collapsible debug drawer with
   decomposition JSON + timing chips.
4. `ConciergeAgent.answer()` includes a `timings` dict on every return
   path. Happy path has `decompose_ms`, `retrieve_ms`, `render_ms`,
   `total_ms`. Short-circuits have at least `total_ms`. Decompose /
   render exceptions still record the failed stage's elapsed time.
5. `pytest tests/call_center/test_concierge.py` → **10 passed**.
