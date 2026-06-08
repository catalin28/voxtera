"""Prompt loader for the call-center pipeline.

All LLM system prompts and user-facing clarification text for the
call-center modules live as ``.md`` files in this package so that
non-engineers can tune wording (and a future admin UI can edit them
in place) without touching Python source.

Public helpers:
  - ``load_prompt(name)`` — returns the raw text of ``<name>.md``.
  - ``load_localised_prompts(name)`` — parses an ``.md`` file with
    ``## <locale>`` sections and ``- <slot>: <text>`` lines into a
    ``{locale: {slot: text}}`` dict. Used by triage.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent

# mtime-keyed cache: prompts HOT-RELOAD when the file changes on disk (the
# admin prompts editor depends on this — no server restart after an edit).
# A stat() per call costs microseconds; text is re-read only on change.
_cache: dict[str, tuple[float, str]] = {}


def load_prompt(name: str) -> str:
    """Read ``<name>.md`` from this package and return its UTF-8 text.

    Hot-reloads on file change (mtime check per call). Uses ``read_bytes``
    + manual decode so Windows newline translation does not silently
    mutate the prompt content.

    HTML comments (``<!-- … -->``) are STRIPPED before the text reaches the
    model: they are editor-facing notes (e.g. "the persona lives in
    concierge_persona.md") that the admin Prompt Editor shows in the raw
    file but that should cost the LLM zero tokens and zero attention.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    mtime = path.stat().st_mtime
    cached = _cache.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    text = path.read_bytes().decode("utf-8")
    text = re.sub(r"<!--.*?-->\n?", "", text, flags=re.DOTALL).strip() + "\n"
    _cache[name] = (mtime, text)
    return text


def load_localised_prompts(name: str) -> dict[str, dict[str, str]]:
    """Parse ``<name>.md`` into ``{locale: {slot: text}}``.

    Grammar (kept deliberately tiny):
      - ``## <locale>`` opens a new locale section.
      - ``- <slot>: <text>`` adds a slot/text entry to the current
        locale. Everything else (blank lines, prose, H1 title, other
        headings) is ignored, so the file can carry human comments.
    """
    raw = load_prompt(name)
    out: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current = stripped[3:].strip().lower()
            out.setdefault(current, {})
            continue
        if current and stripped.startswith("- ") and ":" in stripped:
            key, _, value = stripped[2:].partition(":")
            out[current][key.strip()] = value.strip()
    return out
