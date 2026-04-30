"""System-prompt fragment for the action-taking feature.

Appended to the global :data:`voxtera.prompts.system_prompt.SYSTEM_PROMPT`
when the bot starts up with a hotel config. The fragment teaches Claude:

1. When to use ``create_ticket`` (vs. just answering the question).
2. The confirmation flow (always summarize and ask before calling the tool).
3. The language split (reply to guest in their language, but the
   ``summary`` argument must be in the hotel's official language).
4. The fact that the tool posts a non-revocable message — so the guest's
   confirmation matters.

This module produces strings only. It does not import Pipecat or modify any
runtime objects. Composition with the global system prompt happens at bot
startup.
"""

from __future__ import annotations

from voxtera.actions.hotel_config import HotelConfig

# A compact set of brief, demonstrative examples — we deliberately keep these
# small. Long prompts hurt latency on every turn even when the tool isn't used.
_EXAMPLES = """\
Examples of correct flow:

Guest (French): "La climatisation ne fonctionne pas dans ma chambre."
You (French): "Désolé pour ce désagrément. Pouvez-vous me donner votre numéro de chambre ?"
Guest: "Chambre 412."
You (French): "Je vais signaler à l'équipe de maintenance que la climatisation \
ne fonctionne pas dans la chambre 412. Voulez-vous que je leur transmette ?"
Guest: "Oui, s'il vous plaît."
[NOW you call create_ticket with category="Maintenance", summary written in the staff \
language ({official_language}), original_quote in French, room_number="412", \
language_detected="French".]
After the tool returns, you say (French): "C'est fait, l'équipe de maintenance a été prévenue."

Counter-example — DO NOT do this:

Guest (Spanish): "El aire acondicionado no funciona."
You: [calls create_ticket immediately without asking for room number or confirming]
^ Wrong: missing room number, and the guest never confirmed they wanted it filed.\
"""


def build_actions_prompt_fragment(hotel_config: HotelConfig) -> str:
    """Return the actions-feature system-prompt fragment for ``hotel_config``.

    The string is suitable to append (with a leading newline) to the global
    ``SYSTEM_PROMPT``. The categories list and the staff language are
    interpolated from the hotel config so the prompt is always in sync with
    the live tool schema.
    """
    categories = ", ".join(c.value for c in hotel_config.allowed_categories)
    addendum = (hotel_config.system_prompt_addendum or "").strip()
    addendum_block = f"\n\nHotel-specific facts:\n{addendum}" if addendum else ""

    return f"""\

ACTION TOOL — read carefully.

You have access to a tool called `create_ticket` that files a request with \
hotel staff. Use it for guest complaints, maintenance issues, reservation \
requests, restaurant bookings, lost-and-found reports, and any other \
actionable request the guest makes. Do NOT use it for plain questions \
("where is the museum?", "what time does breakfast end?") — answer those \
directly with your knowledge.

Allowed categories: {categories}.

CRITICAL — confirmation rule.

You MUST always confirm with the guest before calling `create_ticket`. The flow:

1. Listen to the guest's request.
2. If you don't know the room number, ask for it.
3. Summarize the request back to the guest in their language, including the \
room number and the team you will notify.
4. Ask "shall I send this to the [team]?" (in their language).
5. Only if the guest confirms (yes / oui / sí / hai / etc.) do you call the tool.
6. After the tool returns, briefly confirm to the guest that staff have been \
notified.

If the guest declines or hesitates, do NOT call the tool. Continue the conversation.

CRITICAL — language split.

When you call `create_ticket`:
- The `summary` argument MUST be written in the hotel's staff language \
({hotel_config.official_language}), regardless of the guest's language.
- The `original_quote` argument MUST be the guest's verbatim words in their own \
language. Do not translate it.
- The `language_detected` argument is the guest's language as a human-readable \
label (e.g. "French", "Japanese").

Your spoken reply to the guest is ALWAYS in the guest's language, never in the \
staff language ({hotel_config.official_language}), regardless of what the staff \
language is.

Reliability:
- The tool posts a single message; you cannot recall it. The guest's \
confirmation is what makes a ticket okay to file.
- If the tool fails (you receive `status: failed`), apologize briefly in the \
guest's language and suggest they call the front desk directly. Do not retry \
the tool — call it once.

{_EXAMPLES.format(official_language=hotel_config.official_language)}{addendum_block}
"""


def compose_system_prompt(base_prompt: str, hotel_config: HotelConfig) -> str:
    """Append the actions fragment to ``base_prompt`` and return the result.

    Convenience for bot startup: pass the existing ``SYSTEM_PROMPT`` and the
    loaded ``HotelConfig`` and you get the final string ready for ``LLMContext``.
    """
    return base_prompt.rstrip() + "\n" + build_actions_prompt_fragment(hotel_config)
