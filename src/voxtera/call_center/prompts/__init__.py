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

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Read ``<name>.md`` from this package and return its UTF-8 text.

    Uses ``read_bytes`` + manual decode so Windows newline translation
    does not silently mutate the prompt content.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    return path.read_bytes().decode("utf-8")


@lru_cache(maxsize=None)
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
