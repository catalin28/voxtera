# Voice concierge — prompt flow

Companion doc to the **Voice Concierge Prompts** admin page
(`/admin/voice_prompts.html`). Mirrors `docs/call-center/prompt-flow.md`, but
for the real-time voice agent (the "Her"-inspired persona).

## The voice turn

```
Caller (web widget / phone) → Daily room / PSTN dial-in
  │
  ├─ CALL START: greeting from greetings.json  ← editable
  │    spoken instantly, no LLM round-trip; time-neutral, or a
  │    morning/afternoon/evening variant when the browser reports
  │    the guest's timezone (GreetingController)
  │
  └─ guest speaks
       → STT (Gladia Solaria-1; per-utterance language detection)
       → [instant filler — fillers.py, hardcoded, ~100 ms]   (read-only)
       → LLM turn (Claude):
            SYSTEM_PROMPT  (system_prompt.md)                ← editable
            + actions fragment (actions/prompt.py)           (read-only, generated)
            + hotel config (appended at bot startup)
            + RAG retrieval, when enabled (per-turn context)
            + conversation memory (Redis transcript)
       → TTS (guest's language) → caller hears the answer
```

## What's editable, and what isn't

| Surface | File | Editable | Why |
|---|---|---|---|
| System prompt — the "Her" persona | `src/voxtera/prompts/system_prompt.md` | ✅ | Plain markdown, loaded at import by `system_prompt.py`. |
| Startup greetings (31 languages) | `src/voxtera/prompts/greetings.json` | ✅ | Extracted from `greetings.py` (2026-06-06); loaded once at import, validated on save. |
| Instant-ack fillers | `src/voxtera/prompts/fillers.py` | ❌ read-only | Latency-critical (~100 ms budget); deliberately hardcoded Python. |
| Action-taking fragment | `src/voxtera/actions/prompt.py` | ❌ read-only | Python that *generates* the prompt at startup, parameterised per hotel — no single text to edit. |

## Reload semantics — IMPORTANT

Unlike the call-center prompts (hot-reload, "live on the next request"), the
voice bot runs as a **separate subprocess spawned per call** and imports its
prompts **once at startup**:

- Saves apply from the **NEXT CALL** — never mid-call, never per-request.
- The serve.py TTS-test endpoint imports `GREETINGS` at server start, so that
  one admin test page shows old greeting text until a server restart
  (harmless; real calls always get the saved text).

## greetings.json format

```json
{
  "greetings":       { "en": "Welcome to {hotel_name} — …", "fr": "…", … },
  "timed_greetings": { "en": { "morning": "…", "afternoon": "…", "evening": "…" }, … },
  "generic_hotel":   { "en": "our hotel" }
}
```

The optional `{hotel_name}` placeholder is resolved once at bot startup
(`apply_hotel_name` / `resolve_greeting(hotel_name=…)` in `greetings.py`) with
the name from `HotelConfig` — "Welcome to Casa Dell Arte". When the bot runs
without a hotel config, the per-language `generic_hotel` phrase is used
instead ("Welcome to our hotel"). Plain `str.replace`, not `str.format`.

Rules enforced by the save endpoint (`_validate_greetings_json` in serve.py):
valid JSON; both top-level objects present; `"en"` required in `greetings`
(the universal fallback); every value a non-empty string; timed keys limited
to `morning` / `afternoon` / `evening`. A language in `greetings` but missing
from `timed_greetings` is fine — it falls back to the neutral greeting.

Where a language does not lexically distinguish a daypart (French has no
separate "good afternoon"; Korean and Hindi barely daypart greetings), the
variants intentionally repeat — correct usage, not an oversight.

## Editing cautions

- **audio.py fingerprint.** `audio.py` embeds `SYSTEM_PROMPT` as a semantic
  fingerprint of the bot's domain for the STT noise filter. Tone edits are
  fine; do **not** strip the hotel/travel vocabulary wholesale, or the
  filter's baseline drifts. For the same reason the loader reads
  bytes + explicit decode and the save endpoint writes bytes unmodified — no
  newline normalisation.
- **No HTML comments in system_prompt.md.** The call-center loader strips
  `<!-- … -->` editor notes; the voice loader does not. Anything you type in
  the file reaches the LLM verbatim.
- **Persona duplication.** The voice persona (`system_prompt.md`) is separate
  from the call-center persona (`concierge_persona.md`). Aligning or unifying
  them is a product decision — don't do it silently from either editor.

## API

- `GET  /api/admin/voice-prompts` — list with content, descriptions, readonly flags.
- `POST /api/admin/voice-prompts` — save `{name, content}`; whitelist only;
  readonly entries rejected (403); greetings structurally validated;
  timestamped backup to `logs/prompt_backups/` before every write.
- Auth: `X-Admin-Token` header (same token as all admin pages).
