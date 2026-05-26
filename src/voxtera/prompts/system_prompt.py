"""System prompt for Voxtera — the hotel voice concierge persona.

The prompt has three jobs, in tension with one another:

1. Persona. Voxtera is the hotel's concierge, not a generic assistant —
   warm, polished, present. A guest should feel attended to, not processed.
   The PRESENCE section carries this; warmth is expressed through word
   choice and graceful acknowledgement, never through extra talking time.
2. Brevity. Every word of bot output becomes ~330 ms of TTS playback during
   which the leakage_guard correctly silences the user's mic. A 30-word reply
   is a 10-second monologue the user can't speak over; a 12-word reply is a
   4-second one. Trace data showed bot replies routinely exceeding 10 seconds
   before brevity was made a first-class rule. Presence must NOT reopen this:
   the concierge is gracious *and* economical — those are not in conflict.
3. Language consistency. Keep Claude replying in the user's language for the
   whole conversation — language drift is the main multilingual-test failure
   mode.

The prompt text itself lives in ``system_prompt.md`` next to this module and
is loaded at import time. Edit that file to change the prompt; do not embed
the string back into this module. Keeping the prompt in plain text (rather
than a Python literal) makes diffs cleaner, lets non-engineers tune the
wording, and avoids escape/line-continuation noise.

Note: this string is also embedded in ``audio.py`` as a semantic fingerprint
of the bot's domain (hotels, travel, dining, transport) for the STT noise
filter. Keep the hotel/travel domain vocabulary intact when editing, or that
filter's baseline drifts.
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"

# Read as bytes and decode explicitly so we get exactly what's on disk — no
# platform-dependent newline translation. The audio.py fingerprint embeds this
# string, so byte-stability matters.
SYSTEM_PROMPT: str = _PROMPT_PATH.read_bytes().decode("utf-8")
