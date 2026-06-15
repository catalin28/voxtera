# Prompt architecture — consolidation plan

> Status: **proposal / for later**. Nothing here is implemented yet. Captures the
> decision to keep prompts few and organised by the right axis, and the steps to
> get there. Author context: written 2026-06-14 while wiring per-hotel overrides
> and the menu-PDF flow.

## Goal (the one-line idea)

Don't have a prompt per channel. End state should be:

> **1 persona + 2 answer-rule files (voice / text) + rare per-hotel overrides — shared across ALL transports (WhatsApp, web, phone).**

Channel (WhatsApp vs web vs phone) is **transport only**, never a prompt axis.

## Why "one prompt per channel" is the wrong axis

The bot is the same *person* on every channel. What actually changes the wording
is:

1. **Modality** — voice (short, spoken, numbers as words, no markdown) vs text
   (can be longer / formatted). Already handled by the `brief` flag.
2. **Identity** — a specific hotel that genuinely needs a distinct persona.
   Handled by per-hotel override (sparingly).

Mapped onto channels: WhatsApp-call = web-orb = phone are all **voice** → one
prompt. WhatsApp-text = web-chat are **text** → one prompt. So three channels
collapse into two prompts, not three.

The real source of prompt sprawl is **per-hotel**, if every hotel forks every
file. Mitigation: most hotels use the shared persona + a YAML facts addendum;
only flagship/distinct properties get a full persona override.

## Current state (as of 2026-06-14)

### Two brains exist — this is the real duplication

| Brain | Code path | Prompt source | Load behaviour |
|---|---|---|---|
| **Legacy single-LLM** | `pipeline.py` / `bot.py` when `BOT_BRAIN != travel_agent`; `compose_system_prompt(SYSTEM_PROMPT, hotel_config)` | `src/voxtera/prompts/system_prompt.md` | **read ONCE at import** (`system_prompt.py`) → edits need restart / next call |
| **Concierge** (current) | `ConciergePipeline` / `TravelAgentBrain` when `BOT_BRAIN == travel_agent` | `src/voxtera/call_center/prompts/*.md` | **hot-reload** per turn (`load_prompt` mtime check) → edits apply next turn |

All the recent work (entity/name resolver, menu-PDF offers, streaming TTS,
per-hotel overrides) lives **only in the concierge brain**. The legacy brain has
none of it.

### Transports and which brain they use

| Transport | Code | Brain |
|---|---|---|
| WhatsApp call + chat | `whatsapp/call_bot.py`, `whatsapp/webhook.py` | always `travel_agent` (concierge) |
| Web voice orb | Daily pipeline + `TravelAgentBrain` | concierge (per call_bot docstring) |
| Phone / PSTN | `pipeline.py` (Daily dial-in) | **depends on `BOT_BRAIN`** — legacy unless set to travel_agent |

WhatsApp is a separate transport (SmallWebRTC), not part of the Daily/PSTN
diagram in the "Voice Concierge Prompts" admin page.

### Concierge prompt assembly (the keeper)

Built at runtime in `call_center/concierge.py::_with_persona()`:

```
concierge_persona.md                       (WHO — shared)
+ travel_agent_voice_render_brief.md        (voice rules)   ← brief=True
  OR concierge_render.md                     (text rules)    ← brief=False
+ [MENU:] block   (menu_catalog.py, if menus enabled)
+ [OFFER:] block  (image_catalog.py, if images enabled)
+ actions/ticket fragment (actions/prompt.py, if ticketer)
```

Per-hotel override already supported: `load_prompt(name, hotel_id)` prefers
`call_center/prompts/<hotel_id>/<name>.md`, else the shared file. Light per-hotel
facts: `system_prompt_addendum` in `config/hotels/<hotel_id>.yaml`.

## Target architecture

```
call_center/prompts/
  concierge_persona.md            # WHO — shared default
  travel_agent_voice_render_brief.md   # HOW, voice (all voice channels)
  concierge_render.md             # HOW, text  (all text channels)
  concierge_converse.md, triage_questions.md, ...   # pipeline-stage prompts
  <hotel_id>/                     # per-hotel overrides — ONLY files that differ
     concierge_persona.md         # e.g. Çırağan distinct identity
```

- **One brain** (`travel_agent` / ConciergePipeline) for WhatsApp, web AND phone.
- **One persona**, split only by voice/text, plus sparse per-hotel overrides.
- The legacy `system_prompt.md` brain is **retired** (no second persona to keep
  in sync).

## Consolidation plan (do later)

1. **Confirm the phone/PSTN deployment can run `BOT_BRAIN=travel_agent`.**
   Check the Daily/PSTN droplet env; run a test call on the concierge brain.
   Risk: any legacy-only behaviour the phone relied on must exist in the
   concierge (it generally does, and more).
2. **Switch phone/PSTN to `travel_agent`.** Then phone == WhatsApp == web, one
   brain, all the current features (name resolver, menu PDF, per-hotel).
3. **Retire the legacy brain + `system_prompt.md`.** Once nothing runs
   `BOT_BRAIN != travel_agent`: remove (or clearly archive) `voxtera/prompts/
   system_prompt.md`, `compose_system_prompt`, and the legacy branch in
   `pipeline.py`. Keep `actions/prompt.py` (still used by the concierge for
   the ticket fragment).
4. **Repoint or retire the "Voice Concierge Prompts" admin page.** It currently
   edits `system_prompt.md` (legacy). Point it at the concierge files
   (`concierge_persona.md`, `travel_agent_voice_render_brief.md`,
   `concierge_render.md`, and per-hotel overrides) — or remove it to avoid
   editing a file nothing uses.
5. **Document the per-hotel convention** (already started: README in
   `prompts/kempinski_ciragan/`). Rule of thumb: YAML `system_prompt_addendum`
   for facts; a full `<hotel_id>/<name>.md` only when the identity must differ.

## Gotchas to remember

- The admin page warning "read ONCE at bot start — edits apply from next call"
  is **only true for the legacy `system_prompt.md`**. The concierge prompts
  hot-reload per turn.
- Editing `system_prompt.md` does **not** change the WhatsApp/concierge bot.
- Voice vs text is decided by `brief`, not by channel.
