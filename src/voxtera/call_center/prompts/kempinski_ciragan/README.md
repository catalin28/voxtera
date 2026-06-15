# Per-hotel prompt overrides — kempinski_ciragan

Drop a `<name>.md` here to override the matching global prompt for THIS hotel
only. Any prompt you don't put here falls back to the shared one in the parent
`prompts/` folder. Resolution lives in `prompts/__init__.py` (`load_prompt`).

The render path passes `hotel_id`, so these names are the ones worth overriding:

| File to create here              | Overrides (effect)                                  |
|----------------------------------|-----------------------------------------------------|
| `concierge_persona.md`           | persona / tone / language for this property         |
| `travel_agent_voice_render_brief.md` | spoken (voice) answer rules                      |
| `concierge_render.md`            | text/chat answer rules                              |
| `concierge_converse.md`          | chit-chat / recall turns                            |

Notes:
- Channel doesn't matter — WhatsApp and web share prompts. The split is voice
  (brief → `travel_agent_voice_render_brief.md`) vs text (`concierge_render.md`).
- For small per-hotel facts (not a full prompt rewrite), prefer
  `system_prompt_addendum` in `config/hotels/kempinski_ciragan.yaml` instead.
- This README is never loaded as a prompt (only explicitly named `<name>.md`
  files are), so it's safe to keep here.
