# Phase 3 — Ready for Your Review

**Status:** Code written, tests passing, **bot.py NOT modified.** Live bot runs identically to last night.
**Branch:** `feat/action-taking`
**Author:** Claude (overnight prep, 5 review cycles)
**Built on top of:** Phases 1 & 2 (already merged on this branch)

---

## TL;DR

I prepared everything for Phase 3 (Pipecat tool registration + system-prompt update) **without touching `bot.py` or the active system prompt**. New modules, new tests, all green. Your morning task: read this doc, accept or adjust the proposed changes, then apply two small diffs to `bot.py` and verify with a live voice call.

Files added or modified overnight (none of the files below are imported by `bot.py`'s current code):

```
src/voxtera/actions/__init__.py        (extended exports)
src/voxtera/actions/tool.py            NEW — FunctionSchema for create_ticket
src/voxtera/actions/handler.py         NEW — Pipecat function callback
src/voxtera/actions/prompt.py          NEW — system-prompt fragment builder
src/voxtera/actions/integration.py     NEW — wire_actions() helper
tests/test_actions.py                  NEW — 26 unit tests (all passing)
scripts/test_actions_handler.py        NEW — manual smoke test
docs/PHASE3_READY.md                   NEW — this document
```

---

## What each new module does

### `actions/tool.py`
Builds a `pipecat.adapters.schemas.FunctionSchema` named `create_ticket`. The `category` parameter's enum is restricted to the hotel's `allowed_categories` (so Claude can never file a ticket in a category the hotel hasn't enabled). Required parameters: `category`, `summary`, `room_number`, `original_quote`, `language_detected`. Each parameter has a clear description that tells Claude what to put there — including the language split (summary in staff language, original_quote verbatim).

### `actions/handler.py`
The Pipecat function callback. Pipecat invokes it with a `FunctionCallParams` object when Claude calls `create_ticket`. The handler:

1. Coerces and validates the args (length bounds, no null bytes, category is allowed).
2. Builds a `Ticket` dataclass.
3. Calls `sink.send(ticket)` — wrapped in `try/except` even though the sink contract forbids raising (defense in depth for future Freshdesk/Zendesk sinks).
4. Calls `params.result_callback(...)` with one of three statuses: `filed`, `failed`, or `rejected`. The callback itself is wrapped in `try/except` so a Pipecat queue-shutdown can't crash the voice loop.

The handler **never raises**. Tests confirm this for: bad args, sink errors, sink raising unexpectedly, callback raising.

### `actions/prompt.py`
Two functions: `build_actions_prompt_fragment(hotel_config)` and `compose_system_prompt(base, hotel_config)`. The fragment teaches Claude:

- When to use the tool (real requests, not casual Q&A).
- The confirmation rule (always summarize and ask before filing).
- The language split (summary in staff language, original_quote verbatim, spoken reply in guest's language).
- That tickets are non-revocable.
- Failure handling (don't retry, suggest front desk).

It also embeds two examples (correct flow + counter-example) and the hotel's `system_prompt_addendum` if present.

### `actions/integration.py`
The single entry point: `wire_actions(*, llm, context, hotel_config, sink)`. Registers the function with the LLM service AND attaches the schema to the `LLMContext.tools`. Idempotent — calling twice replaces rather than duplicates the create_ticket schema. Defensive against malformed prior tool lists.

### `actions/__init__.py`
Exports the public API. The package now exposes:

```
Category, Ticket
TicketSink, TelegramSink
HotelConfig, load_hotel_config
build_create_ticket_tool, CREATE_TICKET_FUNCTION_NAME
build_actions_prompt_fragment, compose_system_prompt
wire_actions
```

---

## Proposed `bot.py` changes (NOT applied — for your review)

The integration is intentionally one-call. After your morning review, you'd apply this diff manually. The only files that change are `bot.py` and a small block of `pipeline.py` (or the `LLMContext` is built somewhere else — see "Open question" below).

### Patch 1 — wire actions into the pipeline

The `LLMContext` is currently built inside `pipeline.py`'s `build_pipeline()` (line 305–307). The cleanest place to wire the actions feature is just after it. Here's the proposed change:

**File:** `src/voxtera/pipeline.py`
**Around line 305–307**, change:

```python
    # Conversation context. The system prompt does the heavy lifting on the
    # multilingual requirement — see src/voxtera/prompts/system_prompt.py.
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)
```

…to:

```python
    # Conversation context. The system prompt does the heavy lifting on the
    # multilingual requirement — see src/voxtera/prompts/system_prompt.py.
    # When actions are enabled, append the per-hotel actions fragment and
    # register the create_ticket tool.
    if settings.actions_enabled:
        from voxtera.actions import (
            TelegramSink,
            compose_system_prompt,
            load_hotel_config,
            wire_actions,
        )

        hotel_cfg = load_hotel_config(settings.hotel_id)
        system_text = compose_system_prompt(SYSTEM_PROMPT, hotel_cfg)
        sink = TelegramSink.from_env()  # reads TELEGRAM_BOT_TOKEN/CHANNEL_ID
    else:
        hotel_cfg = None
        sink = None
        system_text = SYSTEM_PROMPT

    messages: list[dict[str, str]] = [{"role": "system", "content": system_text}]
    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)

    if settings.actions_enabled and hotel_cfg is not None and sink is not None:
        wire_actions(llm=llm, context=context, hotel_config=hotel_cfg, sink=sink)
```

### Patch 2 — add the feature flag to `Settings`

**File:** `src/voxtera/config.py`
Add a single field to the `Settings` dataclass (anywhere in the body, suggested near `rag_enabled`):

```python
    # Actions: enable the create_ticket LLM tool and route filed tickets to
    # the configured sink (Telegram via TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID).
    actions_enabled: bool = False
```

…and load it in `load_settings()` from `os.environ.get("ACTIONS_ENABLED", "false").lower() == "true"`.

### Patch 3 — add the env var to `.env.example`

**File:** `.env.example`
Add at the bottom:

```
# Actions feature — file tickets via Telegram (or other sinks).
# Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID.
ACTIONS_ENABLED=false
```

That's it. With `ACTIONS_ENABLED=false` (the default), the bot behaves exactly as it does today. With `ACTIONS_ENABLED=true`, the actions pipeline is wired in.

### Open question

In `pipeline.py` the LLM service (`llm = AnthropicLLMService(...)`) is built right above the context block. `wire_actions` needs both `llm` and `context`. The placement above works as long as `llm` is in scope. Confirm visually when you apply the patch — it should be straightforward, but the file is long.

---

## Cycle log (what review found and what I fixed)

**Cycle 1 — Build.**
Wrote tool.py, handler.py, prompt.py, integration.py, updated __init__.py, wrote 22 unit tests.

**Cycle 2 — Lint + tests.**
- Ran `ruff check --select=E,F,I,N,UP,B,SIM`. Two findings on my code: import sort order, unused `wire_actions` import in tests. Both auto-fixed.
- One pre-existing finding on `Category(str, Enum)` (UP042 / use StrEnum) — left alone, that's your existing code and you've already chosen this pattern.
- Ran `pytest`: 22/22 passing.

**Cycle 3 — Plan-doc cross-check.**
- Verified parameter list matches `ACTIONS_FEATURE_PLAN.md` §5 Phase 3 step 1. ✓
- Confirmation flow matches §3.2. ✓
- Language split matches §3.3. ✓
- Categories are constrained to hotel's `allowed_categories` matches §3.1 expectations. ✓
- **One extension to plan, flagged here:** I added `language_detected` as a required tool parameter. The plan's bullet list didn't include it, but the Telegram message format in §3.5 includes "Guest spoke in: …", so the field is needed. Worth confirming in your review.
- **One deliberate omission from plan, flagged here:** the plan's Phase 3 step 2 says "Register the function with the LLM service in `bot.py`". I did NOT modify `bot.py`. Instead I built `wire_actions()` as a helper. You apply the wiring tomorrow after reviewing this doc.

**Cycle 4 — Independent code review (sub-agent).**
Spawned a fresh sub-agent to review the new files cold. It found 2 major and 5 medium issues:

1. **MAJOR** — `integration.py` could crash if `existing.standard_tools` is None or non-iterable. Fixed: defensive guards + try/except + logger warning.
2. **MAJOR** — `handler.py` arg-mapping check too permissive (object with `.get` *attribute* but not callable). Fixed: now requires `callable(get)`.
3. **MEDIUM** — `handler.py` didn't wrap `sink.send` in try/except. The contract says sinks must not raise, but a future buggy sink (Freshdesk/Zendesk) could. Fixed: defense-in-depth wrap, treats raise as `ok=False`.
4. **MEDIUM** — `prompt.py` and `tool.py` used `{language!r}` which produces `'en'` (with quotes) and reads oddly to Claude. Fixed: changed to parenthetical "(en)" notation.
5. **MEDIUM** — `_require_str` had no length validation. Fixed: now takes `max_len` matching the schema's maxLength.
6. **MEDIUM** — `_require_str` accepted null bytes. Fixed: explicitly rejected.
7. **MEDIUM** — no test for `result_callback` raising. Fixed: added `test_handler_survives_callback_raising` and wrapped the callback call in `_safe_callback`.
8. **MINOR** — sub-agent suggested combining two category checks. **Rejected** — they catch two different conditions (invalid Category vs. valid Category not enabled for this hotel), so both are needed.
9. **MINOR** — sub-agent suggested adding instructions for guest-hangup-mid-confirmation. **Rejected** — Pipecat handles disconnect at the pipeline level; instructing Claude is unnecessary and inflates the prompt.

**Cycle 5 — Apply fixes + final pass.**
Applied 7 of the 9 findings. Re-ran tests: 26/26 passing (4 new tests added). Re-ran ruff: clean except for the pre-existing UP042. Re-read each module one more time.

No further issues found. Stopping cycles here per the agreement.

---

## Your morning checklist

In rough order — do them top-to-bottom.

1. **Skim the new files.** ~10 min. Pay particular attention to `prompt.py` — the prompt content is the part that needs your judgement.

2. **Run the tests yourself.** From project root:
   ```bash
   cd /Users/dandinu/ChatGPTPProjects/voxtera
   uv run pytest tests/test_actions.py -v
   ```
   Expected: 26 passing.

3. **Run the manual smoke test for the handler.** This exercises the full path from "fake LLM call" → handler → TelegramSink → live Telegram channel:
   ```bash
   uv run python scripts/test_actions_handler.py
   ```
   Expected: a [Maintenance] post lands in the channel; result payload reads `status: filed`.

4. **Review the proposed `pipeline.py` / `config.py` patches above.** If you accept them as-is, apply them by hand. If you want to tweak the placement or naming, do that.

5. **Set `ACTIONS_ENABLED=true` in `.env`** and run the bot:
   ```bash
   make run
   ```
   Speak to the bot in any language. Try: "the air conditioning in my room is broken, room 412". The bot should:
   - Acknowledge in your language.
   - Summarize back and ask "shall I send this to the maintenance team?"
   - On your confirmation, call `create_ticket` and a Telegram message lands.
   - Confirm aloud that staff have been notified.

6. **Test the negative case.** Ask a plain question: "what time does breakfast start?". The bot should NOT call create_ticket — answer directly.

7. **Test the cancellation case.** Start a complaint, then say "no, never mind" at the confirmation step. No ticket should land.

8. **Test the cross-language case.** Speak in French. Confirm in French. Ticket arrives, but the summary is in English (the hotel's staff language).

If any of these misbehave, the fix is most likely in `prompt.py` — that's the part I couldn't validate against a live model overnight.

---

## What I deliberately did NOT do

- Modify `bot.py` or `pipeline.py`.
- Modify the active `SYSTEM_PROMPT` text.
- Modify `Settings` or `.env.example`.
- Add Phase 4 (session memory for room number caching). The handler currently requires `room_number` in every call; the prompt tells Claude to ask for it. Caching is a Phase 4 optimization.
- Run any live LLM tests. I have no way to make a voice call to your bot.

---

## Risk assessment

**Low risk** for the morning bot.py integration: the wiring is one helper call (`wire_actions`) gated behind a feature flag. Reverting is `ACTIONS_ENABLED=false` or removing the wire_actions call.

**Medium risk** in the prompt content: prompt engineering is empirical. The first few real conversations will reveal if Claude follows the confirmation rule consistently, especially in less-common languages. Budget some iteration time.

**Open observation, not a defect:** the prompt fragment adds ~1.5 KB to the system prompt, increasing per-turn input tokens. At ~$3/M tokens for Sonnet that's negligible (cents per 1000 calls), but worth knowing. We could shrink the examples if costs become a concern.

Sleep well. See you in the morning.
