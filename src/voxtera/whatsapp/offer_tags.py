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

# Complete hidden tags the render may append: photo offers ([OFFER:<id>]) and
# menu-PDF offers ([MENU:<id>]). Neither must ever be spoken aloud.
OFFER_TAG_RE = re.compile(r"\[(?:OFFER|MENU):[^\]]+\]")
_TAG_PREFIXES = ("[OFFER:", "[MENU:")


def split_streamable(text: str) -> tuple[str, str]:
    """Split ``text`` into ``(emit_now, hold_back)`` for incremental TTS.

    Strips any COMPLETE ``[OFFER:<id>]`` / ``[MENU:<id>]`` tags, then holds back
    a trailing fragment ONLY if it could still become one of those tags (a
    prefix of ``[OFFER:`` / ``[MENU:`` or an unclosed ``[OFFER:…``/``[MENU:…``).
    Everything else is safe to speak immediately, so a half-formed hidden tag is
    never voiced.
    """
    text = OFFER_TAG_RE.sub("", text)
    li = text.rfind("[")
    if li != -1:
        tail = text[li:]
        if any(pre.startswith(tail) or tail.startswith(pre) for pre in _TAG_PREFIXES):
            return text[:li], tail
    return text, ""
