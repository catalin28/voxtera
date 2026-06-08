# HANDOFF — build the "Hotel Voice Concierge Prompts" admin page

**Goal:** an admin page like the existing **Call Center Prompts** editor, but for
the prompts of the HOTEL VOICE CONCIERGE (the real-time voice agent with the
"Her"-inspired persona) — same look, same editing workflow, same explanations
per prompt, plus a flow diagram of the voice turn.

Written 2026-06-06 at the end of a working session; this doc contains
everything a fresh session needs. Read it fully before coding.

---

## 1. The reference implementation (already built, copy its patterns)

| Piece | Where |
|---|---|
| Editor page (HTML/JS, dark admin theme, interactive SVG flow diagram, click-a-node-to-open-prompt, selected-prompt highlight, Flow ▾ toggle) | `demo-hotel/admin/call_center_prompts.html` |
| Prompt registry with per-prompt `title` + `description` | `_PROMPT_REGISTRY` in `demo-hotel/serve.py` (near the top, after the audit import) |
| API: list + save | `_handle_admin_prompts_list` / `_handle_admin_prompts_save` in `serve.py`; dispatched at `GET/POST /api/admin/prompts` |
| Admin auth | `self._admin_auth(require_daily=False)` — `X-Admin-Token` header; token stored in localStorage AND sessionStorage under `voxtera_admin_token` (accept both) |
| Save safety | whitelist names only; JSON files validated before write; timestamped backup to `logs/prompt_backups/<file>.<YYYY-MM-DD_HH-MM-SS>.bak` before every save |
| Hot-reload loader (call-center only) | `load_prompt` in `src/voxtera/call_center/prompts/__init__.py` — mtime cache + **strips `<!-- … -->` HTML comments** so editor notes never reach the LLM |
| Portal card | `demo-hotel/admin/index.html` (cards grid) |
| Flow-diagram doc for the call center | `docs/call-center/prompt-flow.md` |

## 2. The voice concierge's prompt surface (what the new page edits)

Inventory (verified 2026-06-06):

1. **`src/voxtera/prompts/system_prompt.md`** — THE main prompt: the
   "Her"-inspired persona (PRESENCE section), brevity rules (every word ≈
   330 ms of TTS that blocks the guest's mic), language-consistency rules.
   Loaded by `system_prompt.py` at import into the `SYSTEM_PROMPT` constant.
   **This is the only safely text-editable file — make it the v1 scope.**
2. `src/voxtera/actions/prompt.py` — Python module producing the
   action-taking fragment (create_ticket flow) appended to SYSTEM_PROMPT at
   bot startup, parameterised by `HotelConfig`. NOT a plain text file.
3. `src/voxtera/prompts/fillers.py` — hardcoded multilingual instant-ack
   fillers ("One moment."). Python, latency-critical, deliberately hardcoded.
4. `src/voxtera/prompts/greetings.py` — hardcoded multilingual greetings
   (~32 KB Python).

**v1 scope: expose only `system_prompt.md`.** For 2-4, either show them
READ-ONLY in the page (content visible, save disabled, description explains
why) or leave them out with a note. A later v2 could migrate greetings/fillers
to JSON data files to make them editable — do NOT attempt that casually; the
filler/greeting loading is latency-critical and used by the voice pipeline.

## 3. Critical caveats (do not skip)

- **Reload semantics differ from the call center.** The voice bot runs as a
  separate subprocess spawned per call (launcher in `serve.py`). It imports
  `SYSTEM_PROMPT` at startup → **edits apply from the NEXT CALL**, not
  mid-call and not hot per-request. Surface this in the page UI (the call
  center page says "live on the next request ✓"; this one must say "applies
  to the NEXT CALL").
- **`audio.py` embeds `SYSTEM_PROMPT` as a semantic fingerprint** for the STT
  noise filter — the hotel/travel domain vocabulary in the prompt calibrates
  that filter. Put a warning in the prompt's registry description: tone edits
  fine; do not strip the hotel/travel vocabulary wholesale.
- `system_prompt.py` reads the file as **bytes + explicit decode** for
  byte-stability (the fingerprint). The save endpoint writes UTF-8 bytes —
  fine — but don't "normalise" line endings.
- The comment-stripping trick (editor notes in `<!-- … -->`) only exists in
  the **call-center** loader. If you add editor notes to `system_prompt.md`,
  you must either (a) add the same stripping to `system_prompt.py`'s load, or
  (b) don't use comments there. (a) is ~3 lines; if you do it, keep the
  read-bytes+decode approach and re-check the audio.py fingerprint note.

## 4. Implementation plan

1. **serve.py — registry.** Add `_VOICE_PROMPT_REGISTRY` next to
   `_PROMPT_REGISTRY`, and a `_voice_prompts_dir()` helper resolving
   `Path(voxtera.prompts.__file__).parent`. Entry for `system_prompt`:
   title "Voice concierge system prompt (the 'Her' persona)", description
   covering: persona/PRESENCE, brevity (TTS-blocking), language consistency,
   the audio.py fingerprint warning, and "applies from the next call".
   Optional read-only entries for fillers/greetings/actions with
   `"readonly": true`.
2. **serve.py — endpoints.** `GET/POST /api/admin/voice-prompts`, cloned from
   the prompts handlers (same `_admin_auth(require_daily=False)`, same backup
   dir `logs/prompt_backups/`, reject saves to readonly entries).
3. **Page.** Copy `call_center_prompts.html` →
   `demo-hotel/admin/voice_prompts.html`. Change: title/header "Hotel Voice
   Concierge Prompts", fetch `/api/admin/voice-prompts`, save-status text
   "saved — applies from the next call ✓", and REPLACE the flow diagram with
   a voice-turn diagram (see §5).
4. **Portal card** in `demo-hotel/admin/index.html`: e.g. icon 🎙️, title
   "Voice Concierge Prompts", description mentioning the "Her" persona and
   next-call reload semantics.
5. **Doc.** Add `docs/voice-prompt-flow.md` mirroring
   `docs/call-center/prompt-flow.md`.
6. **Verify.** `python3 -m py_compile demo-hotel/serve.py`; run the
   call-center test suite untouched
   (`PYTHONPATH=src CONCIERGE_LOG_DIR=/tmp/ig pytest tests/call_center -q`
   — note: in the sandbox first shim `datetime.UTC = datetime.timezone.utc`,
   Python 3.10 there; and ALWAYS set `CONCIERGE_LOG_DIR` or tests pollute the
   user's real `logs/`). Then ask the user to restart the server and check
   the page end-to-end.

## 5. The voice-turn flow diagram (for the page)

Same SVG style as the call-center one (clickable `g.pnode[data-prompt=…]`
nodes, `--accent` stroke for editable prompts, `--border` for infrastructure,
dashed for data/lists). The voice turn:

```
Caller (phone/web) → Daily room / PSTN dial-in
  → STT (Gladia Solaria-1; language detection)
  → [instant filler plays — fillers.py, hardcoded, ~100ms]   (readonly node)
  → LLM turn: SYSTEM_PROMPT (system_prompt.md  ← THE editable node)
              + actions fragment (actions/prompt.py, readonly)
              + hotel config
  → TTS → caller hears the answer
Call start: greeting from greetings.py (readonly node)
```

Annotate: "system_prompt.md is read once at bot start — edits apply from the
next call." If the call-center RAG hand-off exists by then (voice bot calling
the call-center pipeline), add that arrow too — check before drawing.

## 6. Context you'll want in your head

- The user dislikes persona/tone duplication. The call-center side has ONE
  persona file (`concierge_persona.md`) prepended to all its answer writers
  via `_with_persona()` in `concierge.py`. The VOICE persona
  (`system_prompt.md`) is currently SEPARATE from the call-center persona —
  the user knows, and may later want them aligned or unified. Don't unify
  silently; it's a product decision.
- Admin pages share the dark theme (`#0d1117`/`#161b22`, accent `#58a6ff`)
  and the token storage described in §1.
- The user tests by restarting the server and clicking through — give them
  exact URLs and what to expect, and remind them which changes need restart
  vs refresh vs nothing (code=restart, static page=hard-refresh,
  prompt-content=next request / next call).
