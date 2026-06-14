"""Phonetic / fuzzy proper-noun resolution for spoken venue names.

A tourism voice agent must survive an English ASR model mis-hearing foreign
proper nouns: "Tuğra" comes back as "Tura" / "Tula", and the menu lookup then
fails because nothing matches. STT biasing (see ``voxtera.stt``) reduces this
but cannot eliminate it, so this module is the deterministic safety net.

The insight: a hotel's *venues* are a tiny, closed, KNOWN set (≈5 names). So we
don't need the ASR to be perfect — we snap whatever it produced to the nearest
known venue, then the rest of the pipeline routes on the canonical name.

Scope on purpose: ONLY venues are snap targets (they decide which menu/KB doc to
open — i.e. routing). Dish names are deliberately NOT snapped here: there are
hundreds, many near-identical ("Sea Bass" vs "Sea Bream"), so a global dish
snap-list would mis-resolve. Dish questions ride on scoped semantic retrieval
instead. See the design notes in the project memory.

Algorithm: diacritic-fold the utterance, slide 1–3-token windows over it, score
each window against the folded venue list with RapidFuzz Jaro-Winkler (best
metric for short proper nouns — it weights shared prefixes, so "tu…" variants
cluster on "Tuğra"), and replace a window with the canonical venue ONLY when the
match is both confident (≥ ``_THRESHOLD``) and an unambiguous winner over the
runner-up (≥ ``_MARGIN`` ahead). Thresholds were calibrated on real failing
transcripts: genuine mis-hearings score ≥0.83, ordinary words ("menu", "table",
"please") stay ≤0.78, leaving a clean gap.
"""

from __future__ import annotations

import json
import string
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from loguru import logger
from rapidfuzz.distance import JaroWinkler

# Same single source of truth the STT bias list reads (voxtera.stt).
# This file is <repo>/src/voxtera/call_center/entity_resolver.py → parents[3] = <repo>.
_VOCABULARY_PATH = Path(__file__).resolve().parents[3] / "config" / "stt_vocabulary.json"

# Calibrated separation (see module docstring): catch "tula" (0.827) / "toogra"
# (0.84) while rejecting "please" (0.778) and every other common word.
_THRESHOLD = 0.80
# Best venue must beat the runner-up by this much — guards against ambiguous
# snaps between two similar venue names.
_MARGIN = 0.05
# Single tokens shorter than this never attempt a match ("two", "for", "the",
# "you" are all ≤3 chars) — removes the riskiest false positives outright.
_MIN_TOKEN_LEN = 4
# For a MULTI-word venue, every word of the span must align to its venue word at
# least this well. This stops a window from swallowing a neighbour: "at Tuğra"
# can't map to "Tuğra" (word counts differ) and "at fumoir" can't map to
# "Le Fumoir" ("at" vs "le" aligns far below the floor), while the genuine
# mishearing "le fumua" → "Le Fumoir" passes (both words align).
_WORD_FLOOR = 0.72
# Belt-and-braces: never snap a pure function word even if it scored high.
_STOPWORDS = frozenset(
    {
        "please",
        "menu",
        "table",
        "dinner",
        "lunch",
        "breakfast",
        "restaurant",
        "reservation",
    }
)


def _fold(s: str) -> str:
    """Lower-case, strip diacritics and edge punctuation so 'Tuğra', 'tugra'
    and 'Tula.' (trailing period from STT) all compare on equal footing."""
    base = "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    ).lower().strip()
    # strip leading/trailing punctuation from each word, keep word boundaries
    return " ".join(w.strip(string.punctuation) for w in base.split()).strip()


@dataclass(frozen=True)
class _Venue:
    canonical: str  # display name, e.g. "Ruya İstanbul"
    folded: str  # "ruya istanbul"
    folded_words: tuple[str, ...]  # ("ruya", "istanbul")


@lru_cache(maxsize=32)
def _load_venues(hotel_id: str) -> tuple[_Venue, ...]:
    """Load the canonical venue list for ``hotel_id`` from the vocab file.

    Reads ``hotel_proper_nouns[hotel_id].venues``. Cached per hotel_id. Returns
    an empty tuple (logged) when the file/entry is missing, so resolution
    becomes a no-op rather than an error.
    """
    try:
        data = json.loads(_VOCABULARY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning("[entity-resolver] vocabulary unreadable: {}", exc)
        return ()
    entry = (data.get("hotel_proper_nouns") or {}).get(hotel_id)
    if isinstance(entry, dict):
        names = entry.get("venues") or []
    elif isinstance(entry, list):  # legacy flat list → treat all as venues
        names = entry
    else:
        names = []
    venues: list[_Venue] = []
    for n in names:
        f = _fold(str(n))
        if f:
            venues.append(_Venue(canonical=str(n), folded=f, folded_words=tuple(f.split())))
    if not venues:
        logger.info("[entity-resolver] no venues for hotel_id={!r}", hotel_id)
    return tuple(venues)


def _score_span(
    folded_words: tuple[str, ...], venues: tuple[_Venue, ...]
) -> tuple[_Venue | None, float, float]:
    """Best + runner-up score of one folded token-span over the venues.

    Single-token spans may match either a venue's full name or any one of its
    words ("fumoir" → "Le Fumoir", "ruya" → "Ruya İstanbul"). Multi-token spans
    only match a venue with the SAME word count, and only when every word aligns
    to its venue word above ``_WORD_FLOOR`` — so a window can't swallow a
    neighbouring word it doesn't actually correspond to.
    """
    best: _Venue | None = None
    best_score = 0.0
    second = 0.0
    n = len(folded_words)
    for v in venues:
        if n == 1:
            tok = folded_words[0]
            sc = JaroWinkler.normalized_similarity(tok, v.folded)
            for w in v.folded_words:
                cand = JaroWinkler.normalized_similarity(tok, w)
                if cand > sc:
                    sc = cand
        else:
            if len(v.folded_words) != n:
                continue
            pairs = [
                JaroWinkler.normalized_similarity(a, b)
                for a, b in zip(folded_words, v.folded_words, strict=True)
            ]
            sc = (sum(pairs) / n) if min(pairs) >= _WORD_FLOOR else 0.0
        if sc > best_score:
            second = best_score
            best, best_score = v, sc
        elif sc > second:
            second = sc
    return best, best_score, second


@dataclass(frozen=True)
class Substitution:
    """One applied canonicalization, for tracing/debug."""

    heard: str  # the original span as transcribed ("tula")
    canonical: str  # what it was replaced with ("Tuğra")
    score: float


def canonicalize_venues(utterance: str, hotel_id: str | None) -> tuple[str, list[Substitution]]:
    """Snap mis-heard venue names in ``utterance`` to their canonical forms.

    Returns ``(rewritten_utterance, substitutions)``. When nothing resolves (or
    no hotel/venue list), returns the utterance unchanged with an empty list, so
    callers can use it unconditionally.
    """
    if not utterance or not hotel_id:
        return utterance, []
    venues = _load_venues(hotel_id)
    if not venues:
        return utterance, []

    tokens = utterance.split()
    if not tokens:
        return utterance, []

    # Score every 1–3-token window; collect confident, unambiguous candidates.
    candidates: list[tuple[float, int, int, _Venue, str]] = []  # score,start,len,venue,raw
    for start in range(len(tokens)):
        for span_len in (1, 2, 3):
            end = start + span_len
            if end > len(tokens):
                break
            raw = " ".join(tokens[start:end])
            folded = _fold(raw)
            if not folded:
                continue
            if span_len == 1 and (len(folded) < _MIN_TOKEN_LEN or folded in _STOPWORDS):
                continue
            folded_words = tuple(folded.split())
            if not folded_words:
                continue
            venue, score, second = _score_span(folded_words, venues)
            if venue is None or score < _THRESHOLD or (score - second) < _MARGIN:
                continue
            # already exactly the canonical name → nothing to fix
            if raw == venue.canonical:
                continue
            candidates.append((score, start, span_len, venue, raw))

    if not candidates:
        return utterance, []

    # Greedily accept highest-scoring, then longest, non-overlapping spans.
    candidates.sort(key=lambda c: (c[0], c[2]), reverse=True)
    taken: list[bool] = [False] * len(tokens)
    chosen: list[tuple[int, int, _Venue, str, float]] = []
    for score, start, span_len, venue, raw in candidates:
        if any(taken[start : start + span_len]):
            continue
        for i in range(start, start + span_len):
            taken[i] = True
        chosen.append((start, span_len, venue, raw, score))

    if not chosen:
        return utterance, []

    # Rebuild the utterance with replacements applied (left to right).
    chosen.sort(key=lambda c: c[0])
    out: list[str] = []
    subs: list[Substitution] = []
    i = 0
    replace_at = {c[0]: c for c in chosen}
    while i < len(tokens):
        if i in replace_at:
            _, span_len, venue, raw, score = replace_at[i]
            out.append(venue.canonical)
            subs.append(Substitution(heard=raw, canonical=venue.canonical, score=round(score, 3)))
            i += span_len
        else:
            out.append(tokens[i])
            i += 1

    rewritten = " ".join(out)
    if subs:
        logger.info(
            "[entity-resolver] hotel_id={!r} canonicalized {}",
            hotel_id,
            ", ".join(f"{s.heard!r}→{s.canonical!r}({s.score})" for s in subs),
        )
    return rewritten, subs
