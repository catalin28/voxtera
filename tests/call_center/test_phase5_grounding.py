"""Phase 5 — grounding / anti-fabrication guards.

Most of Phase 5 is enforced by prompt rules (persona + render), which can't be
unit-tested deterministically. What we CAN lock down is the data-side
invariant behind "canonical restaurant list per tenant; de-dupe name variants":
each hotel's venue list must contain no duplicates once folded (lower-cased,
diacritics stripped) — otherwise the entity resolver could snap to either of
two equivalent spellings (Ruya / Rüya) and the render would present an
inconsistent name.
"""

from __future__ import annotations

import json
from pathlib import Path

from voxtera.call_center.entity_resolver import _VOCABULARY_PATH, _fold


def _vocab() -> dict:
    return json.loads(Path(_VOCABULARY_PATH).read_text(encoding="utf-8"))


def _venue_lists() -> dict[str, list[str]]:
    """hotel_id -> venue list, for every hotel that declares venues."""
    out: dict[str, list[str]] = {}
    entries = (_vocab().get("hotel_proper_nouns") or {}).items()
    for hotel_id, entry in entries:
        if isinstance(entry, dict) and entry.get("venues"):
            out[hotel_id] = list(entry["venues"])
    return out


def test_some_hotel_declares_venues() -> None:
    """Sanity: the canonical-list invariant is actually exercised."""
    assert _venue_lists(), "expected at least one hotel with a venues list"


def test_venue_lists_have_no_fold_duplicates() -> None:
    """No tenant's venues collapse to the same folded form (Ruya/Rüya guard)."""
    for hotel_id, venues in _venue_lists().items():
        seen: dict[str, str] = {}
        for name in venues:
            folded = _fold(name)
            assert folded not in seen, (
                f"hotel {hotel_id!r}: venues {seen.get(folded)!r} and {name!r} "
                f"collapse to the same canonical form {folded!r} — de-dupe them"
            )
            seen[folded] = name


def test_venue_names_are_non_empty_strings() -> None:
    for hotel_id, venues in _venue_lists().items():
        for name in venues:
            assert isinstance(name, str) and name.strip(), (
                f"hotel {hotel_id!r} has an empty/blank venue entry"
            )
