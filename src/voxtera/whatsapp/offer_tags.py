"""Photo-offer tag helpers for the voice call path.

The hotel render can append a hidden ``[OFFER:<id>]`` tag (the prompt places it
at the very end of a reply) signalling that a photo is available. On a VOICE
call we must never speak that tag, yet we still want to stream the visible text
to TTS sentence-by-sentence as it arrives. ``split_streamable`` does exactly
that: it emits everything safe to speak now and holds back only a fragment that
could still grow into a tag.

Kept transport-free (no pipecat/WebRTC imports) so it is cheap to unit-test
independently of ``call_bot``.
"""

from __future__ import annotations

import re

# A complete tag, e.g. "[OFFER:tugra_menu]". Mirrors image_catalog._OFFER_RE.
OFFER_TAG_RE = re.compile(r"\[OFFER:[^\]]+\]")
OFFER_PREFIX = "[OFFER:"


def split_streamable(text: str) -> tuple[str, str]:
    """Split ``text`` into ``(emit_now, hold_back)`` for incremental TTS.

    Strips any COMPLETE ``[OFFER:<id>]`` tags, then holds back a trailing
    fragment ONLY if it could still become an offer tag (a prefix of
    ``[OFFER:`` or an unclosed ``[OFFER:…``). Everything else is safe to speak
    immediately, so a half-formed hidden tag is never voiced.
    """
    text = OFFER_TAG_RE.sub("", text)
    li = text.rfind("[")
    if li != -1:
        tail = text[li:]
        if OFFER_PREFIX.startswith(tail) or tail.startswith(OFFER_PREFIX):
            return text[:li], tail
    return text, ""
