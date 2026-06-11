"""ConciergePipeline — Phase 3 orchestrator (Slice B).

Wires together the five Slice-A modules into the canonical call-flow:

    classify_escalation
        → load_session
        → decompose
        → triage  (may short-circuit with a clarification question)
        → route   (may short-circuit asking for geography / hotel resolution)
        → execute_path  (compound retrieval for KB paths; placeholders
                         for web / destination / hybrid until later phases)
        → render
        → session.append_turn + session.save

Each step is dependency-injected so unit tests run fully offline.

Decision contract (returned by ``ConciergePipeline.run``):

    {
      "session_id":     str,
      "utterance":      str,
      "path":           str,           # "escalate" | "clarify" | router PATH_*
      "reason":         str,           # short audit string
      "escalation":     dict | None,   # classifier verdict when escalated
      "clarification":  dict | None,   # {question, slot} when triage asks
      "decomposition":  dict | None,
      "router":         dict | None,
      "retrieval":      dict | None,
      "answer":         str | None,
      "timings":        dict[str, float],
    }

Why a new class and not an in-place refactor of ConciergeAgent?
ConciergeAgent ships the Phase-2c surface (decompose -> compound -> render)
that the existing /api/concierge demo and tests depend on. The new
pipeline composes those primitives differently (5 modules instead of 2),
so keeping them as siblings avoids breaking the legacy surface while
the demo UI is being wired up.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from voxtera.call_center.classifier import EscalationClassifier
from voxtera.call_center.compound import CompoundAndDiscovery
from voxtera.call_center.decompose import QueryDecomposer
from voxtera.call_center.kb_config import REGION_ALIASES, canonical_region
from voxtera.call_center.resolver import HotelResolver
from voxtera.call_center.router import (
    PATH_BROAD,
    PATH_DESTINATION,
    PATH_ESCALATE,
    PATH_HOTEL_RESOLVE,
    PATH_HYBRID,
    PATH_NEEDS_GEOGRAPHY,
    PATH_SCOPED,
    PATH_WEB,
    SourceRouter,
)
from voxtera.call_center.session import SessionStore, build_transcript, new_session_id
from voxtera.call_center.triage import Triage
from voxtera.call_center.web_retriever import WebRetriever

# Paths that resolve against the hotel KB (Qdrant).
_KB_PATHS = {PATH_SCOPED, PATH_BROAD}

# Fallback requirements for a scoped query about a resolved hotel that arrived
# with no specific ask ("tell me about X", "how about X?"). The decomposer is
# inconsistent here — sometimes it emits ["hotel overview", "amenities", ...],
# sometimes []. An empty list makes CompoundAndDiscovery short-circuit with
# `empty_requirements` and the concierge fails closed even though the hotel is
# known. Injecting a generic overview makes the scoped path robust to that.
_SCOPED_DEFAULT_REQUIREMENTS = ("hotel overview", "amenities", "facilities")


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


def _placeholder_answer(path: str, language: str) -> str:
    """Localised acknowledgement for paths whose retrievers ship later."""
    msgs = {
        PATH_DESTINATION: {
            "en": (
                "I can answer destination questions like that — "
                "full destination KB ships in the next release."
            ),
            "tr": "Bu tür destinasyon sorularını yakında tam olarak yanıtlayabileceğim.",
        },
        PATH_WEB: {
            "en": "That needs a live web lookup — the web layer goes live in the next release.",
            "tr": "Bu canlı bir web aramasi gerektiriyor — web katmanı yakında devreye alınacak.",
        },
        PATH_HYBRID: {
            "en": (
                "That mixes hotel data and a live web check — "
                "the hybrid path lights up in the next release."
            ),
            "tr": (
                "Bu otel verisi ile canlı web aramasını birleştiriyor — "
                "hibrit yol yakında aktif olacak."
            ),
        },
        PATH_HOTEL_RESOLVE: {
            "en": "Which hotel exactly are you asking about?",
            "tr": "Tam olarak hangi otelden bahsediyorsunuz?",
        },
        PATH_NEEDS_GEOGRAPHY: {
            "en": "Which destination are you thinking of?",
            "tr": "Hangi destinasyonu düşünüyorsunuz?",
        },
        PATH_ESCALATE: {
            "en": "Let me connect you to a colleague who can help with that right away.",
            "tr": "Sizi hemen bu konuda yardımcı olabilecek bir çalışma arkadaşıma bağlayayım.",
        },
    }
    by_lang = msgs.get(path, {"en": "Let me check on that."})
    return by_lang.get(language) or by_lang.get("en") or "Let me check on that."


# Progressive narrowing: when a BROAD query returns this many strong matches,
# behave like a travel agent and ask ONE differentiating question instead of
# reading out the whole list. (DEFAULT_MAX_HOTELS is 5, so 4+ = "a lot".)
_NARROW_THRESHOLD = 4
_MAX_CLARIFICATIONS = 2  # shared budget with Triage

# Vector-store name detection: a confident single hotel needs a strong absolute
# e5 score AND a clear margin over the runner-up (good matches sit ~0.77-0.84).
_DETECT_MIN_SCORE = 0.78
_DETECT_MARGIN = 0.04
# A literal hotel-NAME match scores high in absolute terms (0.83+); a generic
# vibe query ("spa hotel") tops out ~0.80 because e5 compresses generic matches.
# So a strong absolute score is a confident name hit even when a runner-up is
# close — e.g. two same-name hotels ("Casa Dell Arte" Residance vs Arts&Leisure)
# sit near-tied at 0.82-0.84, which the margin gate alone would wrongly reject.
_DETECT_STRONG_SCORE = 0.82
# ...but the strong-score path STILL needs a small gap. A nameless follow-up
# ("do they have spa?") makes many hotels tie near-perfectly (e.g. 0.827 vs
# 0.826) and would otherwise be wrongly "detected", hijacking the active hotel.
# A genuine name match keeps a modest lead (Casa Dell Arte: 0.838 vs 0.821 =
# 0.017); a generic cluster does not (~0.001). 0.012 separates the two.
_DETECT_STRONG_MARGIN = 0.012
# Generic words in hotel names that aren't distinctive enough to prove the guest
# actually NAMED a hotel (so "from the beach" doesn't match "...Beach Resort").
_NAME_GENERIC_TOKENS = frozenset(
    {
        "hotel",
        "otel",
        "resort",
        "resorts",
        "suites",
        "suite",
        "residence",
        "residences",
        "residance",
        "residances",
        "spa",
        "beach",
        "bay",
        "park",
        "garden",
        "club",
        "palace",
        "grand",
        "royal",
        "collection",
        "boutique",
        "the",
        "by",
        "of",
        "and",
        "de",
        "la",
        "el",
        "pearl",
        "city",
    }
)


def _detected_name_in_utterance(hotel_name: str, utterance: str) -> bool:
    """True if the detected hotel's name actually appears in the utterance.

    Name detection exists to catch a hotel NAME the decomposer missed — so the
    name must be present. A high vector score with NO name overlap is just a
    content match (e.g. a restaurant-heavy query matching a restaurant-dense
    hotel) and must NOT hijack the active hotel. Returns True when the name is
    all-generic (can't verify) so the score gate still decides.
    """
    words = set(re.findall(r"\w+", (utterance or "").lower()))
    distinctive = [
        t
        for t in re.findall(r"\w+", (hotel_name or "").lower())
        if len(t) >= 3 and t not in _NAME_GENERIC_TOKENS
    ]
    if not distinctive:
        return True  # nothing distinctive to check — defer to the score gate
    return any(t in words for t in distinctive)


def _narrowing_question(decomposition: dict[str, Any], region: str = "") -> tuple[str, str]:
    """Pick the single most useful differentiating question + slot, localised.

    Priority (Architecture §Phase 5): budget → children ages → location pref —
    EXCEPT when no geography is known at all (zero-context "I need a hotel"):
    a travel agent asks WHERE before how much, so geography leads (defect D2,
    2026-06-07). Only en/tr are localised (matching _no_match_answer /
    _placeholder_answer); other languages fall back to English.
    """
    lang = (decomposition.get("language") or "en").lower()

    def _t(en: str, tr: str) -> str:
        return tr if lang == "tr" else en

    has_geo = bool(
        (region or "").strip()
        or any(
            isinstance(decomposition.get(k), str) and decomposition.get(k).strip()
            for k in ("region", "city", "district")
        )
    )
    if not has_geo:
        return "geography", _t(
            "Happy to help — which destination are you thinking of?",
            "Yardımcı olmaktan memnuniyet duyarım — hangi destinasyonu düşünüyorsunuz?",
        )
    if not decomposition.get("budget_tier") and not decomposition.get("budget_signal"):
        # Honest phrasing: this fires BEFORE results are presented, so it must
        # not claim options were already found (defect D3, 2026-06-07).
        return "budget", _t(
            "To find the right fit — what's your budget range?",
            "Size en uygun seçeneği bulabilmem için — bütçe aralığınız nedir?",
        )
    if decomposition.get("traveller_type") == "family" and not decomposition.get("children_ages"):
        return "children_ages", _t(
            "A few of these fit — how old are the children, so I can match the kids' facilities?",
            "Bunlardan birkaçı uygun — çocuk olanaklarını eşleştirebilmem "
            "için çocuklar kaç yaşında?",
        )
    return "location_pref", _t(
        "I have a few matches — would you prefer beachfront, or somewhere closer to the city?",
        "Birkaç eşleşme var — sahil kenarında mı yoksa şehre yakın bir yer mi tercih edersiniz?",
    )


# Adults-only hotels must never be recommended on a family / with-children
# search ("Perge Hotels Adult Only +18" surfaced twice on child-friendly
# queries — defect D4, 2026-06-07). v1 detects the policy from the hotel NAME
# (the corpus reliably brands these "Adult Only" / "+18"); replace with a
# payload attribute check when the ingest pipeline exposes one.
_ADULTS_ONLY_NAME_RE = re.compile(r"adults?\s*[-–]?\s*only|\+\s*18\b|\b18\s*\+", re.IGNORECASE)
_CHILD_CONTEXT_RE = re.compile(
    r"\bkids?\b|child|toddler|baby|babies|family|families|çocuk|aile|bebek|niñ|enfant|kinder",
    re.IGNORECASE,
)


def _wants_children(decomposition: dict[str, Any]) -> bool:
    """True when the request implies children will be staying."""
    if (decomposition.get("traveller_type") or "").lower() == "family":
        return True
    if decomposition.get("children_ages"):
        return True
    blob = " ".join(
        [str(r) for r in (decomposition.get("requirements") or [])]
        + [str(decomposition.get("intent") or "")]
    )
    return bool(_CHILD_CONTEXT_RE.search(blob))


def _drop_adults_only(retrieval: dict[str, Any], decomposition: dict[str, Any]) -> None:
    """In-place: remove adults-only hotels from a family/child retrieval."""
    hotels = (retrieval or {}).get("hotels") or []
    if not hotels or not _wants_children(decomposition):
        return
    kept = [
        h
        for h in hotels
        if not _ADULTS_ONLY_NAME_RE.search(str((h.get("payload") or {}).get("hotel_name") or ""))
    ]
    if len(kept) != len(hotels):
        logger.info(
            "adults-only filter dropped {} hotel(s) from a family/child query",
            len(hotels) - len(kept),
        )
        retrieval["hotels"] = kept
        retrieval["count"] = len(kept)


# ── Last-results referent resolution (defects D9/D10, dialog tests 2026-06-07) ──
# After the bot presents a hotel LIST, guests refer back to it the way humans
# do: "the first one", "is the pool heated?", "compare the top two". Those
# turns carry no hotel name, so ES name-resolution can't help — the LIST is
# the referent. The session keeps the last presented list (`last_results`)
# and these helpers bind such turns to it before routing.

_ORDINAL_REF_RE = re.compile(
    r"\b(first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th"
    r"|ilk|birinci|ikinci|üçüncü|dördüncü|beşinci|sonuncu)\s+"
    r"(one|hotel|option|property|choice|otel\w*|seçenek\w*)\b",
    re.IGNORECASE,
)
_ORDINAL_TO_INDEX = {
    "first": 0,
    "1st": 0,
    "ilk": 0,
    "birinci": 0,
    "second": 1,
    "2nd": 1,
    "ikinci": 1,
    "third": 2,
    "3rd": 2,
    "üçüncü": 2,
    "fourth": 3,
    "4th": 3,
    "dördüncü": 3,
    "fifth": 4,
    "5th": 4,
    "beşinci": 4,
    "last": -1,
    "sonuncu": -1,
}


def _match_results_by_name(
    text: str, mention: str, results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Results whose distinctive name tokens appear in the utterance/mention."""
    out: list[dict[str, Any]] = []
    for r in results:
        toks = [
            t
            for t in re.findall(r"\w+", (r.get("name") or "").lower())
            if len(t) >= 3 and t not in _NAME_GENERIC_TOKENS
        ]
        if toks and any(t in text or t in mention for t in toks):
            out.append(r)
    return out


def _bind_list_referent(
    decomposition: dict[str, Any], session: dict[str, Any], pick: dict[str, Any]
) -> None:
    decomposition["hotel_id"] = pick["hotel_id"]
    # Already bound to a known id — clear the mention so the router doesn't
    # send the turn back through ES re-resolution.
    decomposition["hotel_mention"] = None
    session["active_hotel_id"] = pick["hotel_id"]
    if pick.get("location"):
        session["active_hotel_location"] = pick["location"]  # D19 anchor
    logger.info("list-referent bound to {!r} ({})", pick.get("name"), pick["hotel_id"])


def _resolve_list_referent(
    utterance: str, decomposition: dict[str, Any], session: dict[str, Any]
) -> str | None:
    """Bind this turn to the last presented hotel list when it refers to it.

    Returns: "list_ordinal" / "list_name" / "list_single" when bound,
    "list_ambiguous" when a bare scoped follow-up could mean several of the
    presented hotels (caller should ask WHICH, enumerating the names), or
    None when the turn doesn't reference the list.
    """
    results = session.get("last_results") or []
    if not results or decomposition.get("hotel_id"):
        return None
    text = (utterance or "").lower()
    mention = (decomposition.get("hotel_mention") or "").lower()

    # 1. Positional reference: "the first one", "ikinci otel", "last option".
    m = _ORDINAL_REF_RE.search(text)
    if m:
        idx = _ORDINAL_TO_INDEX.get(m.group(1).lower())
        if idx is not None and -len(results) <= idx < len(results):
            _bind_list_referent(decomposition, session, results[idx])
            return "list_ordinal"

    # 2. The guest named one of the presented hotels (even partially).
    named = _match_results_by_name(text, mention, results)
    if len(named) == 1:
        _bind_list_referent(decomposition, session, named[0])
        return "list_name"

    # 3. Bare scoped follow-up right after a list ("is the pool heated?").
    if (
        decomposition.get("query_type") == "scoped"
        and not mention
        and not session.get("active_hotel_id")
    ):
        if len(results) == 1:
            _bind_list_referent(decomposition, session, results[0])
            return "list_single"
        return "list_ambiguous"
    return None


def _list_clarification(results: list[dict[str, Any]], language: str) -> str:
    """Warm, concierge-style 'which of these did you mean?' enumeration.

    Spoken phrasing, not a database prompt. Turkish deliberately avoids the
    mi/mı question particle after hotel names — vowel harmony can't be done
    safely on arbitrary foreign names, and the 'hangisi' form reads naturally
    without it.
    """
    names = [r.get("name") or "?" for r in results[:3]]
    lang = (language or "en").lower()
    seps = {
        "tr": (", ", " veya "),
        "es": (", ", " o "),
        "fr": (", ", " ou "),
        "de": (", ", " oder "),
        "en": (", ", " or "),
    }
    mid, last = seps.get(lang, seps["en"])
    opts = names[0] if len(names) == 1 else mid.join(names[:-1]) + last + names[-1]
    templates = {
        "tr": "Tabii, hemen bakayım — {} otellerinden hangisini soruyorsunuz?",
        "es": "Con gusto le ayudo — ¿de cuál de estos se trata: {}?",
        "fr": "Avec plaisir — de quel hôtel s'agit-il : {} ?",
        "de": "Sehr gerne — welches dieser Hotels meinen Sie: {}?",
        "en": "Happy to check that for you — which of these did you have in mind: {}?",
    }
    return templates.get(lang, templates["en"]).format(opts)


def _no_match_answer(language: str, region: str) -> str:
    """Deterministic fail-closed reply when retrieval returned 0 hotels.

    Palace voice: own the gap, invite ONE next step. Kept deterministic
    (never invents hotels), but phrased like a person, not an error code.
    """
    region = (region or "").strip().title()  # "paris" → "Paris" in spoken text
    where = f" in {region}" if region else ""
    where_tr = f" {region} bölgesinde" if region else ""
    msgs = {
        "en": (
            f"I don't have a hotel{where} that does justice to everything "
            "you've asked for. Tell me which part matters most — or let's ease "
            "one of the criteria — and I'll take another look."
        ),
        "tr": (
            f"İsteğinizin her detayını karşılayan bir otel{where_tr} maalesef elimde yok. "
            "Sizin için en önemli olan hangisi — ya da bir kriteri biraz esnetelim mi? "
            "Hemen tekrar bakayım."
        ),
    }
    return msgs.get(language) or msgs["en"]


# Explicit "please search the web" requests. Phrase-matched (not bare "internet",
# which would false-fire on "online check-in" / "wifi"). When detected, the
# concierge re-runs the PREVIOUS question on the web instead of the literal ask.
_WEB_REQUEST_PHRASES = (
    "search the internet",
    "search on the internet",
    "search on internet",
    "search internet",
    "search online",
    "search it online",
    "search the web",
    "search web",
    "web search",
    "look it up online",
    "look online",
    "look it up on the internet",
    "check online",
    "check the internet",
    "on the internet",
    "google it",
    "google this",
    "can you google",
    # Turkish
    "internette ara",
    "internetten ara",
    "internetten bak",
    "web'de ara",
    "çevrimiçi ara",
)


# Function/question words stripped before ES name detection so a sentence like
# "How far is Casa Dell Arte from the beach?" resolves on the distinctive name
# tokens ("casa dell arte") rather than letting "beach" pull in a wrong
# "...Beach Resort". Content nouns (beach/spa/pool) are KEPT — many hotels carry
# them in their name (e.g. "Crystal Tat Beach").
_RESOLVE_STOPWORDS = frozenset(
    {
        "how",
        "far",
        "is",
        "are",
        "was",
        "the",
        "a",
        "an",
        "from",
        "to",
        "in",
        "at",
        "of",
        "for",
        "what",
        "where",
        "when",
        "which",
        "who",
        "do",
        "does",
        "did",
        "can",
        "could",
        "would",
        "will",
        "i",
        "you",
        "we",
        "me",
        "my",
        "our",
        "your",
        "it",
        "this",
        "that",
        "there",
        "near",
        "around",
        "and",
        "or",
        "please",
        "tell",
        "about",
        "know",
        "much",
        "away",
        "us",
        "have",
        "has",
        # turkish function / question words
        "nasıl",
        "nerede",
        "ne",
        "kadar",
        "mı",
        "mi",
        "mu",
        "mü",
        "için",
        "var",
        "bir",
        "bu",
        "şu",
        "ile",
        "den",
        "dan",
    }
)


def _clean_for_resolution(utterance: str) -> str:
    toks = [
        t for t in re.split(r"\W+", (utterance or "").lower()) if t and t not in _RESOLVE_STOPWORDS
    ]
    return " ".join(toks)


def _is_web_search_request(utterance: str) -> bool:
    u = (utterance or "").lower()
    return any(p in u for p in _WEB_REQUEST_PHRASES)


# Short affirmations / refusals used to accept or decline an offer the bot just
# made ("…would you like me to check online?"). Kept short so a substantive
# message that merely starts with "yes" ("yes, but show me Antalya hotels")
# doesn't get swallowed.
_AFFIRM_TOKENS = (
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "please",
    "go ahead",
    "do it",
    "sounds good",
    "yes please",
    "please do",
    # Turkish
    "evet",
    "tabii",
    "olur",
    "lütfen",
    "tamam",
)
_NEGATE_TOKENS = ("no", "nope", "nah", "don't", "do not", "hayır", "yok", "gerek yok")
# Phrases that mark the bot's own answer as an offer to LOOK SOMETHING UP (so the
# next turn knows a bare "yes" means "yes, go fetch it"). The warm persona often
# phrases this as "look into" / "find out" rather than literally "online", so we
# match those too — otherwise a "yes" falls through and the bot fabricates a
# promise it never fulfils.
_WEB_OFFER_KEYWORDS = (
    "online",
    "internet",
    "web",
    "çevrimiçi",
    "internette",
    "internetten",
    "look it up",
    "look that up",
    "look up",
    "look into",
    "find out",
    "find you",
    "get you the",
    "pull together",
    "pull up",
    "search for",
    "look for",
    # Turkish
    "araştır",
    "bakabilir",
    "bulabilir",
)


def _is_affirmation(utterance: str) -> bool:
    u = (utterance or "").strip().lower().strip(".!?, ")
    # Polite acceptances run longer than a bare "yes" — "Yes, please look into
    # it" is five words and must still accept (live defect: the 4-word cap
    # bounced it back into a KB no-match). Cap at 8; anything longer, or
    # anything that pivots ("yes, but what about…"), is a NEW request.
    if not u or len(u.split()) > 8:
        return False
    if "?" in (utterance or "") or " but " in f" {u} " or " ama " in f" {u} ":
        return False
    if any(neg in u.split() for neg in ("no", "nope", "nah")) or any(
        neg in u for neg in ("don't", "do not", "hayır", "gerek yok")
    ):
        return False
    return any(tok in u for tok in _AFFIRM_TOKENS)


def _is_refusal(utterance: str) -> bool:
    u = (utterance or "").strip().lower().strip(".!?, ")
    if not u or len(u.split()) > 4:
        return False
    return any(neg in u.split() for neg in ("no", "nope", "nah")) or any(
        neg in u for neg in ("no thanks", "don't", "do not", "hayır", "gerek yok")
    )


def _answer_offers_web(answer: str) -> bool:
    """True when the bot's answer ended with an offer to search online."""
    a = (answer or "").lower()
    return "?" in a and any(k in a for k in _WEB_OFFER_KEYWORDS)


# When the guest asks about REVIEWS, point the web search straight at review
# sites instead of the open web. (Google reviews live in Maps and are hard to
# fetch, but TripAdvisor + the OTAs carry rich guest reviews.)
_REVIEW_DOMAINS = [
    "tripadvisor.com",
    "booking.com",
    "expedia.com",
    "trustpilot.com",
    "holidaycheck.com",
    "agoda.com",
    "hotels.com",
    "google.com",
]
_REVIEW_TERMS = (
    "review",
    "reviews",
    "rating",
    "ratings",
    "tripadvisor",
    "trip advisor",
    "feedback",
    "opinion",
    "opinions",
    "what people think",
    "what guests",
    "didn't like",
    "did not like",
    "complaints",
    "criticism",
    # Turkish
    "yorum",
    "yorumlar",
    "puan",
    "değerlendirme",
    "şikayet",
)


def _is_review_query(decomposition: dict[str, Any], utterance: str) -> bool:
    """True if the turn is asking about guest reviews / ratings."""
    blob = " ".join(
        [
            (utterance or ""),
            " ".join((decomposition or {}).get("requirements") or []),
            (decomposition or {}).get("intent") or "",
        ]
    ).lower()
    return any(t in blob for t in _REVIEW_TERMS)


def _ask_what_to_search(language: str) -> str:
    return (
        "İnternette tam olarak neyi aramamı istersiniz?"
        if language == "tr"
        else "Sure — what would you like me to look up online?"
    )


def _web_caveat(language: str) -> str:
    # A light, natural disclaimer — not a robotic bracketed footnote. Kept short
    # so it doesn't undercut the warm concierge tone when read aloud.
    return (
        "Bunu hızlı bir web aramasından buldum, dilerseniz teyit edebiliriz."
        if language == "tr"
        else "I pulled that from a quick web search, so we can double-check it if you'd like."
    )


def _no_web_answer(language: str) -> str:
    return (
        "Bunu şu anda web'de bulamadım — biraz sonra tekrar deneyebilir misiniz?"
        if language == "tr"
        else "I couldn't find that on the web just now — could you try again in a moment?"
    )


def _format_sources(web: dict[str, Any]) -> str:
    sources = (web or {}).get("sources") or []
    lines = []
    for s in sources[:2]:
        label = s.get("title") or s.get("url")
        if label:
            lines.append(f"• {label}")
    return ("\n\nSources:\n" + "\n".join(lines)) if lines else ""


def _web_answer(web: dict[str, Any], language: str, *, synth: str | None = None) -> str:
    """Build a grounded spoken reply from a web result: answer + caveat.

    No source list — this is read aloud in a voice system, so citations are
    noise. The UI still shows sources in the web-search card. `synth` is an
    optional LLM-cleaned answer that replaces the raw (often self-contradictory)
    retriever blob.
    """
    # The synth already weaves a natural "worth confirming" note into its warm
    # reply, so don't bolt a separate caveat on. Only the raw-fallback path (no
    # synth) needs the appended disclaimer.
    if synth:
        return synth
    answer = (web or {}).get("answer")
    if not answer:
        sources = (web or {}).get("sources") or []
        answer = (sources[0].get("snippet") if sources else "") or ""
    if not answer:
        return _no_web_answer(language)
    return f"{answer}\n\n{_web_caveat(language)}"


def _hotel_facts_text(kb: dict[str, Any] | None, limit: int = 5) -> str:
    """Flatten a scoped KB result into a short 'what the hotel's guide says' block
    for the hybrid synth — the hotel name plus its evidence passages."""
    hotels = (kb or {}).get("hotels") or []
    if not hotels:
        return ""
    h = hotels[0]
    name = (h.get("payload") or {}).get("hotel_name") or h.get("hotel_id") or "the hotel"
    lines = [f"Hotel: {name}"]
    for label, chunk in list((h.get("evidence") or {}).items())[:limit]:
        text = (chunk.get("text_en") or chunk.get("text") or "").strip()
        if text:
            lines.append(f"- {label}: {text[:300]}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _hybrid_answer(
    hotel_part: str, web: dict[str, Any], language: str, *, synth: str | None = None
) -> str:
    """Combine a hotel-KB answer with a live web lookup (proactive hybrid path).

    No source list (voice). `synth` replaces the raw web blob when an LLM has
    cleaned it into one coherent answer.
    """
    web_ans = synth or (web or {}).get("answer")
    parts: list[str] = []
    if hotel_part:
        parts.append(hotel_part)
    if web_ans:
        lead = (
            "Yakınlarda (web aramasından): " if language == "tr" else "Nearby (from a web search): "
        )
        # Synth weaves its own "worth confirming" note; only append the caveat to
        # the raw-fallback web blob.
        tail = "" if synth else "\n\n" + _web_caveat(language)
        parts.append(lead + web_ans + tail)
    if not parts:
        return _no_web_answer(language)
    return "\n\n".join(parts)


def _escalation_answer(lang: str) -> str:
    """Localised hand-off line for the escalation short-circuit."""
    return {
        "tr": ("Sizi hemen bu konuda yardımcı olabilecek bir çalışma arkadaşıma aktarıyorum."),
        "es": "Le pongo enseguida con un compañero que puede ayudarle con eso.",
        "fr": ("Je vous mets tout de suite en relation avec un collègue qui pourra vous aider."),
        "de": "Ich verbinde Sie sofort mit einem Kollegen, der Ihnen dabei helfen kann.",
    }.get(lang, "Let me connect you to a colleague who can help with that right away.")


class ConciergePipeline:
    """End-to-end orchestrator for the call-center concierge."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        classifier: EscalationClassifier,
        decomposer: QueryDecomposer,
        triage: Triage | None = None,
        router: SourceRouter | None = None,
        compound: CompoundAndDiscovery | None = None,
        render_fn: Any | None = None,
        resolver: HotelResolver | None = None,
        web_retriever: WebRetriever | None = None,
        web_synth_fn: Any | None = None,
        converse_fn: Any | None = None,
        web_query_fn: Any | None = None,
        property_kb: Any | None = None,
        property_ticketer: Any | None = None,
    ) -> None:
        self._sessions = session_store
        self._classifier = classifier
        self._decomposer = decomposer
        self._triage = triage or Triage()
        self._router = router or SourceRouter()
        self._compound = compound
        self._render_fn = render_fn
        # Optional LLM that rewrites a raw web-search blob into ONE clean,
        # non-contradictory spoken answer. None = use the raw retriever answer.
        self._web_synth_fn = web_synth_fn
        # Optional LLM that answers conversational/meta/recall turns from the
        # transcript (no retrieval). None = a deterministic fallback reply.
        self._converse_fn = converse_fn
        # Optional LLM that rewrites the conversation into ONE web search query
        # (resolves pronouns + the real hotel name from the dialog, independent of
        # ES/decomposition). None = fall back to the heuristic query builder.
        self._web_query_fn = web_query_fn
        # Live web lookup for PATH_WEB / PATH_HYBRID. None disables the web
        # paths (they fall back to the honest "ships later" placeholder).
        self._web = web_retriever
        # Optional injected resolver (offline tests / custom backends). When
        # None we build the real Elasticsearch-backed resolver per call.
        self._resolver = resolver
        # P1.4 "one brain": the property KB (per-hotel SQLite guide retriever,
        # see call_center.property_kb). When a request carries a hotel_id
        # scope, KB retrieval reads the property's guide instead of the
        # Qdrant travel listings — same decompose/triage/render machinery.
        self._property_kb = property_kb
        # Telegram ticket creation for actionable hotel-mode turns — the port
        # of the legacy create_ticket tool (see call_center.property_actions).
        self._property_ticketer = property_ticketer
        # Per-run property scope; set by run() from the request's hotel_id.
        self._property_hotel_id: str | None = None
        self._turn_utterance = ""

    async def run(
        self,
        *,
        utterance: str,
        session_id: str | None = None,
        region: str | None = None,
        brief: bool = False,
        hotel_id: str | None = None,
    ) -> dict[str, Any]:
        utterance = (utterance or "").strip()
        # When the user explicitly selects "All regions" the caller sends
        # region="" (empty string).  We distinguish that from region=None
        # (not provided) so we can suppress the decomposer's inferred region.
        self._user_region_override = region
        # Property scope (P1.4): this concierge IS one hotel. KB retrieval
        # reads that property's guide (see _run_kb); name resolution and
        # geography clarifications are moot and collapse to scoped retrieval.
        self._property_hotel_id = (hotel_id or "").strip() or None
        # The raw utterance is the best guide-retrieval query (the guide is
        # ingested as Q&A-ish prose; decomposed requirement keywords lose the
        # question shape). Stashed for _run_kb's property branch.
        self._turn_utterance = utterance
        # Voice channel sets brief=True → the render step uses the short,
        # spoken-style prompt (travel_agent_voice_render_brief.md). Read in _render.
        self._brief = brief
        sid = session_id or new_session_id()
        t_start = time.perf_counter()
        timings: dict[str, float] = {}
        # Per-turn debug trace of every vector-store / ES call (what query went
        # where). Reset each turn; surfaced in the result as `trace`. Safe because
        # serve.py builds a fresh pipeline per request and turns run sequentially.
        self._trace: dict[str, list[dict[str, Any]]] = {"qdrant": [], "es": []}

        if not utterance:
            return self._finish(
                sid=sid,
                utterance=utterance,
                path="empty",
                reason="empty_utterance",
                answer="I didn't catch that — could you say it again?",
                t_start=t_start,
                timings=timings,
            )

        # Property fast path (P1.4): when the bot IS one hotel, decompose/
        # triage/routing are pure latency — there is nothing to extract or
        # route. One retrieval + ONE render LLM call, like the legacy hotel
        # brain, with escalation classified concurrently. (~2x faster turns.)
        if self._property_hotel_id:
            return await self._run_property_turn(
                sid=sid, utterance=utterance, t_start=t_start, timings=timings
            )

        # 1+2+3 fan-out: classify runs in parallel with (load_session -> decompose).
        # Decompose needs session context (pending_slots merge, active_region),
        # so it can't run truly independent of session_load; but classify is
        # fully independent and is the biggest single LLM cost on the critical
        # path. Running them concurrently saves ~classify_ms on the happy
        # (non-escalate) path. On escalate we discard the decompose result —
        # the wasted token spend is negligible and the latency win is large.
        t_concurrent = time.perf_counter()

        async def _classify_leg() -> dict[str, Any]:
            t0 = time.perf_counter()
            v = await self._classifier.classify(utterance)
            timings["classify_ms"] = _ms(time.perf_counter() - t0)
            return v

        async def _session_decompose_leg() -> tuple[dict[str, Any], dict[str, Any], str]:
            t0 = time.perf_counter()
            sess = await self._sessions.load(sid)
            timings["session_load_ms"] = _ms(time.perf_counter() - t0)
            # Region handling distinguishes THREE inputs:
            #   - a concrete region ("Antalya")  -> scope to it
            #   - explicit "" ("All regions")     -> clear any stale scope + flag
            #   - None (not provided)             -> leave session as-is
            # The empty-string case is what makes "All regions" work: without
            # this it stays falsy and a previous turn's region lingers, pinning
            # the search to the wrong place (e.g. a Bodrum hotel searched in Antalya).
            if region:
                sess["active_region"] = region
                sess["all_regions"] = False
            elif region == "":
                sess["active_region"] = None
                sess["all_regions"] = True
            decompose_input_local = utterance
            if sess.get("pending_slots"):
                prior_utt = None
                for turn in reversed(sess.get("history") or []):
                    if turn.get("is_clarification") and turn.get("utterance"):
                        prior_utt = turn["utterance"]
                        break
                if prior_utt:
                    decompose_input_local = f"{prior_utt}\nFollow-up: {utterance}"
                sess["pending_slots"] = []
            ctx_local = {
                "active_region": sess.get("active_region"),
                "active_hotel_id": sess.get("active_hotel_id"),
                "language": sess.get("language"),
                # Full prior dialogue (current utterance not yet appended), so the
                # decomposer resolves follow-ups against the real conversation.
                "transcript": build_transcript(sess.get("history")),
            }
            t0 = time.perf_counter()
            decomp = await self._decomposer.decompose(decompose_input_local, ctx_local)
            timings["decompose_ms"] = _ms(time.perf_counter() - t0)
            return sess, decomp, decompose_input_local

        verdict, (session, decomposition, _decompose_input) = await asyncio.gather(
            _classify_leg(),
            _session_decompose_leg(),
        )
        timings["concurrent_pre_ms"] = _ms(time.perf_counter() - t_concurrent)

        # Property scope: pin the session to the property every turn. The
        # router then treats the hotel as resolved (scoped follow-ups work)
        # and as satisfied geography (no "which region?" dead-ends).
        if self._property_hotel_id:
            session["active_hotel_id"] = self._property_hotel_id

        if verdict.get("escalate"):
            # Localised — decomposition ran concurrently, so the detected
            # language is available even on the escalation short-circuit.
            esc_lang = (decomposition.get("language") or session.get("language") or "en").lower()
            answer = _escalation_answer(esc_lang)
            return self._finish(
                sid=sid,
                utterance=utterance,
                path=PATH_ESCALATE,
                reason="escalation_classifier",
                escalation=verdict,
                answer=answer,
                t_start=t_start,
                timings=timings,
            )

        # Accept/decline a web-search OFFER the bot made last turn. When the
        # previous answer asked "…would you like me to check online?", a bare
        # "yes"/"sure"/"evet" must accept it — the user shouldn't have to repeat
        # "search online". We pop the flag so it never lingers into later turns.
        pending_web_offer = bool(session.pop("pending_web_offer", False))
        if self._web is not None and pending_web_offer and _is_affirmation(utterance):
            # last_question still holds the original question (a bare "yes" never
            # overwrote it), so re-run THAT online.
            return await self._handle_web_request(sid, utterance, session, t_start, timings)
        if pending_web_offer and _is_refusal(utterance):
            lang = (session.get("language") or "en").lower()
            ack = (
                "Tabii, başka bir konuda yardımcı olabilir miyim?"
                if lang == "tr"
                else "No problem. Is there anything else I can help with?"
            )
            await self._sessions.append_turn(
                session,
                utterance=utterance,
                decomposition=None,
                reason="web_offer_declined",
                answer=ack,
                is_clarification=False,
            )
            await self._sessions.save(session)
            return self._finish(
                sid=sid,
                utterance=utterance,
                path="clarify",
                reason="web_offer_declined",
                answer=ack,
                t_start=t_start,
                timings=timings,
            )

        # Explicit "search the web" request: re-run the PREVIOUS question online
        # (hybrid if a hotel is active) instead of trying to answer the literal
        # meta-request from the KB. This is how "How far is X?" → "can you search
        # online?" becomes a live web lookup of the original question.
        if self._web is not None and _is_web_search_request(utterance):
            return await self._handle_web_request(sid, utterance, session, t_start, timings)

        # Conversational / meta / recall ("hi", "thanks", "what did I ask you?",
        # "can you repeat?"): answer from the conversation history, NOT the hotel
        # KB. Without this, such turns get embedded as a search query and match
        # garbage hotels on literal tokens. Does not overwrite last_question.
        if (decomposition.get("query_type") or "").lower() == "conversational":
            return await self._handle_converse(
                sid, utterance, session, decomposition, t_start, timings
            )

        # Remember the substantive question so a later "search online" can re-run it.
        session["last_question"] = utterance

        # Promote a newly extracted region into the session so subsequent
        # turns don't have to re-state it. A region named in the utterance
        # overrides an "all regions" selection (the caller got specific).
        new_region = decomposition.get("region") or decomposition.get("city")
        if new_region:
            session["active_region"] = new_region
            session["all_regions"] = False

        # Backfill session language from the first decomposed turn.
        if decomposition.get("language") and not session.get("language"):
            session["language"] = decomposition["language"]

        # Deterministic named-hotel detection (LLM-independent). The decomposer
        # sometimes misses a hotel name — especially foreign ones like
        # "Casa Dell Arte" — leaving hotel_mention null, which then wrongly
        # scopes to a stale session hotel. ES is good at exact name matching, so
        # for a specific-hotel query with no extracted mention we probe the raw
        # utterance; a confident (dominant) match locks onto that hotel and
        # overrides any stale session scope. Skipped for broad/recommendation
        # queries to avoid the extra lookup on the common path.
        if (
            self._compound is not None
            and self._property_hotel_id is None  # one-property scope: nothing to detect
            and not decomposition.get("hotel_mention")
            and not decomposition.get("hotel_id")
            and (decomposition.get("query_type") or "") in ("scoped", "comparison")
        ):
            t0 = time.perf_counter()
            detected = await self._detect_named_hotel(utterance)
            timings["hotel_detect_ms"] = _ms(time.perf_counter() - t0)
            if detected:
                decomposition["hotel_id"] = detected
                session["active_hotel_id"] = detected

        # 4. Triage — may ask one clarification question and short-circuit.
        t0 = time.perf_counter()
        triage_decision = self._triage.assess(decomposition, session)
        timings["triage_ms"] = _ms(time.perf_counter() - t0)
        if triage_decision.get("ask"):
            session["pending_slots"] = list(triage_decision.get("pending_slots") or [])
            await self._sessions.append_turn(
                session,
                utterance=utterance,
                decomposition=decomposition,
                reason=triage_decision.get("reason", "clarification"),
                answer=triage_decision.get("question"),
                is_clarification=True,
            )
            await self._sessions.save(session)
            return self._finish(
                sid=sid,
                utterance=utterance,
                path="clarify",
                reason=triage_decision.get("reason", "clarification"),
                decomposition=decomposition,
                clarification={
                    "question": triage_decision.get("question"),
                    "slot": triage_decision.get("slot"),
                    "language": triage_decision.get("language"),
                },
                answer=triage_decision.get("question"),
                t_start=t_start,
                timings=timings,
            )

        # 4b. List-referent resolution (D9/D10): "the first one" / a name from
        # the last presented list / a bare scoped follow-up binds to that list
        # BEFORE routing, so the turn becomes a normal scoped query instead of
        # a fresh search or a dead-end. Ambiguous follow-ups get a clarification
        # that ENUMERATES the candidates rather than a bare "which hotel?".
        referent = _resolve_list_referent(utterance, decomposition, session)
        if referent == "list_ambiguous":
            lang = (decomposition.get("language") or session.get("language") or "en").lower()
            question = _list_clarification(session.get("last_results") or [], lang)
            await self._sessions.append_turn(
                session,
                utterance=utterance,
                decomposition=decomposition,
                reason="list_ambiguous",
                answer=question,
                is_clarification=True,
            )
            await self._sessions.save(session)
            return self._finish(
                sid=sid,
                utterance=utterance,
                path="clarify",
                reason="list_ambiguous",
                decomposition=decomposition,
                clarification={"question": question, "slot": "hotel_choice", "language": lang},
                answer=question,
                t_start=t_start,
                timings=timings,
            )

        # 5. Route.
        t0 = time.perf_counter()
        decision = self._router.route(decomposition, session)
        timings["route_ms"] = _ms(time.perf_counter() - t0)
        path = decision.get("path", PATH_BROAD)

        # Property scope: there is exactly one hotel, so resolving a name
        # mention against the travel portfolio or asking "which region?" are
        # both dead-ends — collapse them to a scoped guide lookup. (A guest
        # naming a DIFFERENT hotel still gets a guide-grounded answer/no-match
        # rather than information about a property we don't represent here.)
        if self._property_hotel_id and path in (PATH_HOTEL_RESOLVE, PATH_NEEDS_GEOGRAPHY):
            path = PATH_SCOPED
            decision = {
                "path": PATH_SCOPED,
                "sources": ["property_kb"],
                "reason": "property_scope",
                "needs": None,
            }

        # A broad/multi-hotel recommendation means the caller has moved off any
        # specific hotel — drop the stale active_hotel_id so it can't shadow a
        # later scoped follow-up (and so the next turn isn't poisoned by it).
        # (Not under property scope, where the property IS the session hotel.)
        if path == PATH_BROAD and not self._property_hotel_id:
            session.pop("active_hotel_id", None)
            session.pop("active_hotel_location", None)

        # 6. Execute path.
        retrieval: dict[str, Any] | None = None
        answer: str | None = None

        # 6a. Hotel resolution — resolve the name mention to a hotel_id,
        #     then proceed as a scoped KB query filtered to that hotel.
        if path == PATH_HOTEL_RESOLVE:
            hotel_mention = (decomposition.get("hotel_mention") or "").strip()
            if hotel_mention:
                t0 = time.perf_counter()
                if self._resolver is not None:
                    resolution = await self._resolver.resolve(hotel_mention)
                else:
                    import aiohttp as _aio

                    async with _aio.ClientSession() as _resolve_http:
                        resolution = await HotelResolver(session=_resolve_http).resolve(
                            hotel_mention
                        )
                timings["resolve_ms"] = _ms(time.perf_counter() - t0)
                self._trace_es(
                    "resolve",
                    hotel_mention,
                    decision=resolution.get("decision"),
                    hotel_id=resolution.get("hotel_id"),
                )
                if resolution.get("decision") == "auto_resolve" and resolution.get("hotel_id"):
                    # Promote to scoped query with the resolved hotel_id.
                    decomposition["hotel_id"] = resolution["hotel_id"]
                    session["active_hotel_id"] = resolution["hotel_id"]
                    path = PATH_SCOPED
                    decision["path"] = PATH_SCOPED
                    decision["reason"] = "hotel_resolved_inline"

        if path in _KB_PATHS:
            t0 = time.perf_counter()
            # D9 — a comparison right after a list compares the hotels ON the
            # list. Re-running discovery returned a different set than the one
            # on screen, so prose and cards diverged.
            if decomposition.get("query_type") == "comparison":
                retrieval = await self._compare_last_results(utterance, decomposition, session)
            if retrieval is None:
                retrieval = await self._run_kb(decomposition, session, path)
            timings["retrieve_ms"] = _ms(time.perf_counter() - t0)
            if path == PATH_BROAD:
                _drop_adults_only(retrieval, decomposition)

            # Idea 1 — semantic fallback. If the structured path found nothing,
            # search the RAW utterance directly. Requirement extraction can drop
            # the hotel name or over-narrow; the full sentence keeps everything
            # the caller said, and the vector store often recognises a named
            # hotel even when the decomposer missed it. (Skipped under property
            # scope: the property branch already searched the raw utterance, and
            # falling back to the Qdrant travel listings would cross the
            # portfolio boundary.)
            if not ((retrieval or {}).get("hotels")) and self._property_hotel_id is None:
                t0 = time.perf_counter()
                fb = await self._semantic_fallback(utterance, decomposition, session)
                timings["fallback_ms"] = _ms(time.perf_counter() - t0)
                if fb is not None:
                    retrieval = fb
                    if path == PATH_BROAD:
                        _drop_adults_only(retrieval, decomposition)

            # PORTFOLIO BOUNDARY — hotel recommendations come ONLY from the
            # knowledge base: these are the hotels the agency actually sells.
            # A KB miss on a hotel search must fail closed (the no-match reply
            # invites the guest to ease a criterion) and must NEVER be rescued
            # with internet hotels the agency cannot book. (An earlier web
            # rescue here recommended off-portfolio properties — removed
            # 2026-06-07 by business rule. The web remains in play for
            # restaurants, activities, weather and transport via the
            # web/hybrid paths.)

            # Progressive narrowing — only on broad searches with many strong
            # matches, only once per session, and only within the shared
            # clarification budget. Acts like an agent narrowing the field
            # instead of dumping 5 hotels. Skipped when the guest already gave
            # 2+ concrete requirements: "quiet spa hotel with kids' club and
            # sea view" IS the narrowing — interjecting "what's your budget?"
            # before showing anything reads as ignoring the request (defect
            # D2, 2026-06-07; the requirements themselves filter the field).
            clar_count = int(session.get("clarification_count") or 0)
            if (
                path == PATH_BROAD
                and len((retrieval or {}).get("hotels") or []) >= _NARROW_THRESHOLD
                and len(decomposition.get("requirements") or []) < 2
                and not session.get("narrowed")
                and clar_count < _MAX_CLARIFICATIONS
            ):
                if getattr(self, "_user_region_override", None) is not None:
                    narrow_region = self._user_region_override
                else:
                    narrow_region = (
                        decomposition.get("region") or session.get("active_region") or ""
                    )
                slot, question = _narrowing_question(decomposition, narrow_region)
                session["narrowed"] = True
                session["pending_slots"] = [slot]
                await self._sessions.append_turn(
                    session,
                    utterance=utterance,
                    decomposition=decomposition,
                    reason="progressive_narrowing",
                    answer=question,
                    is_clarification=True,
                )
                await self._sessions.save(session)
                return self._finish(
                    sid=sid,
                    utterance=utterance,
                    path="clarify",
                    reason="progressive_narrowing",
                    decomposition=decomposition,
                    router=decision,
                    retrieval=retrieval,
                    clarification={
                        "question": question,
                        "slot": slot,
                        "language": decomposition.get("language") or "en",
                    },
                    answer=question,
                    t_start=t_start,
                    timings=timings,
                )

            if answer is None:  # web rescue above may have answered already
                t0 = time.perf_counter()
                answer = await self._render(utterance, decomposition, retrieval, session)
                timings["render_ms"] = _ms(time.perf_counter() - t0)

        elif path in (PATH_WEB, PATH_DESTINATION) and self._web is not None:
            # Pure live-web question (events, weather, local operators, hours).
            # PATH_DESTINATION also lands here for now: the destination KB is a
            # later phase, and a "ships in the next release" placeholder is a
            # dead-end mid-conversation. Destination questions (itineraries,
            # "which regions for historical sites?") answer well from the live
            # web via the dialog-aware query rewrite + synth. When the
            # destination KB ships, give PATH_DESTINATION its own branch again.
            t0 = time.perf_counter()
            dq = await self._web_query_from_dialog(utterance, session)
            rev = _REVIEW_DOMAINS if _is_review_query(decomposition, utterance) else None
            web = await self._web.discover(utterance, decomposition, query=dq, include_domains=rev)
            timings["web_ms"] = _ms(time.perf_counter() - t0)
            retrieval = {"web": web}
            lang = (decomposition.get("language") or session.get("language") or "en").lower()
            answer = _web_answer(
                web, lang, synth=await self._synth_web(utterance, web, lang, session=session)
            )

        elif path == PATH_HYBRID and self._web is not None:
            # Hotel KB + live web (e.g. "is there a dive shop near my hotel?").
            t0 = time.perf_counter()
            kb = await self._run_kb(decomposition, session, PATH_SCOPED)
            timings["retrieve_ms"] = _ms(time.perf_counter() - t0)
            t0 = time.perf_counter()
            hotel_name, location = self._hotel_web_context(kb, decomposition, session)
            dq = await self._web_query_from_dialog(utterance, session)
            rev = _REVIEW_DOMAINS if _is_review_query(decomposition, utterance) else None
            web = await self._web.discover(
                utterance,
                decomposition,
                hotel_name=hotel_name,
                location=location,
                query=dq,
                include_domains=rev,
            )
            timings["web_ms"] = _ms(time.perf_counter() - t0)
            retrieval = {
                "hotels": (kb or {}).get("hotels") or [],
                "web": web,
                "reason": "hybrid",
                "region": (kb or {}).get("region"),
            }
            lang = (decomposition.get("language") or session.get("language") or "en").lower()
            # ONE combined answer: feed the hotel's own guide facts AND the web
            # result to a single synth so it weaves them into a coherent concierge
            # reply. This avoids the old two-part glue, which produced a double
            # greeting AND offered to "search online" right above the results it
            # had already fetched.
            hotel_facts = _hotel_facts_text(kb)
            combined = await self._synth_web(
                utterance, web, lang, hotel_facts=hotel_facts, session=session
            )
            if combined:
                answer = combined
            else:
                # No synth wired → fall back to the two-part composition.
                hotel_part = (
                    await self._render(utterance, decomposition, kb, session)
                    if (kb or {}).get("hotels")
                    else ""
                )
                answer = _hybrid_answer(hotel_part, web, lang)

        else:
            # Paths whose retrievers ship in later phases (destination KB), or
            # web/hybrid with no web backend wired — honest acknowledgement.
            lang = (decomposition.get("language") or session.get("language") or "en").lower()
            answer = _placeholder_answer(path, lang)

        # If the answer ended with an offer to look something up (any path), arm
        # the flag so a bare "yes" next turn actually runs the web search instead
        # of falling through to the conversational path (which would just promise
        # to do it). Cleared on the next turn whether or not it's accepted.
        if self._web is not None and _answer_offers_web(answer):
            session["pending_web_offer"] = True

        # Remember what was just PRESENTED so next turn's "the first one" /
        # "compare those two" / bare follow-up can refer back to it (D9/D10).
        # ~5 × {hotel_id, name} (~300 bytes) inside the existing Redis session
        # record — negligible next to the history ring buffer.
        presented = [
            {
                "hotel_id": h.get("hotel_id"),
                "name": str((h.get("payload") or {}).get("hotel_name") or ""),
                "location": str(
                    (h.get("payload") or {}).get("district")
                    or (h.get("payload") or {}).get("region")
                    or ""
                ),
            }
            for h in ((retrieval or {}).get("hotels") or [])
            if h.get("hotel_id")
        ]
        # Region coherence (D19): once the guest is talking about a specific
        # hotel, every later recommendation ("dinner near the hotel") must
        # anchor to where THAT HOTEL is — not to a region mentioned earlier in
        # conversation. Keep the active hotel's true location in the session.
        active_id = session.get("active_hotel_id")
        for h in presented:
            if active_id and h["hotel_id"] == active_id and h["location"]:
                session["active_hotel_location"] = h["location"]
                break
        if presented:
            # A scoped drill-down into ONE member of the current list ("does
            # the first one have wifi?") must NOT clobber the list — the guest
            # is still talking about the set ("compare the two of them").
            prev_ids = {r.get("hotel_id") for r in (session.get("last_results") or [])}
            is_drill_down = (
                len(presented) == 1 and presented[0]["hotel_id"] in prev_ids and len(prev_ids) > 1
            )
            if not is_drill_down:
                session["last_results"] = presented[:5]

        # 7. Persist turn.
        await self._sessions.append_turn(
            session,
            utterance=utterance,
            decomposition=decomposition,
            reason=decision.get("reason", path),
            answer=answer,
            is_clarification=False,
        )
        await self._sessions.save(session)

        return self._finish(
            sid=sid,
            utterance=utterance,
            path=path,
            reason=decision.get("reason", path),
            decomposition=decomposition,
            router=decision,
            retrieval=retrieval,
            answer=answer,
            t_start=t_start,
            timings=timings,
        )

    # ---------- internal helpers ----------

    async def _compare_last_results(
        self,
        utterance: str,
        decomposition: dict[str, Any],
        session: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Build a comparison retrieval over the JUST-PRESENTED hotels (D9).

        Picks the hotels the guest named (if ≥2 match the last list) or the
        top two presented, fetches each one's own passages, and returns them
        as the retrieval result so the render compares exactly what's on
        screen. Returns None to fall back to normal discovery (no list in
        session, retriever missing, or no chunks found).
        """
        results = session.get("last_results") or []
        if (
            len(results) < 2
            or self._compound is None
            or not hasattr(self._compound, "fetch_hotel_chunks")
        ):
            return None
        named = _match_results_by_name(
            (utterance or "").lower(),
            (decomposition.get("hotel_mention") or "").lower(),
            results,
        )
        pool = named if len(named) >= 2 else results[:2]
        query = " ".join(decomposition.get("requirements") or []) or "overview amenities dining"
        hotels: list[dict[str, Any]] = []
        for r in pool[:3]:
            self._trace_qdrant("compare", [query], hotel_id=r["hotel_id"], rerank=True)
            chunks = await self._compound.fetch_hotel_chunks(
                hotel_id=r["hotel_id"], query=query, region="", k=4
            )
            if not chunks:
                continue
            evidence: dict[str, Any] = {}
            for i, c in enumerate(chunks):
                label = c.get("category") or f"passage_{i + 1}"
                if label in evidence:
                    label = f"{label}_{i + 1}"
                evidence[label] = c
            hotels.append(
                {
                    "hotel_id": r["hotel_id"],
                    "score": float(chunks[0].get("score", 0.0)),
                    "payload": {
                        "hotel_name": chunks[0].get("hotel_name") or r.get("name") or "",
                        "district": chunks[0].get("district", ""),
                        "region": chunks[0].get("region", ""),
                    },
                    "evidence": evidence,
                }
            )
        if not hotels:
            return None
        reqs = list(decomposition.get("requirements") or [])
        return {
            "region": session.get("active_region") or "",
            "requirements": reqs,
            "normalized_requirements": reqs,
            "top_score": hotels[0]["score"],
            "count": len(hotels),
            "hotels": hotels,
            "missing_requirements": [],
            "reason": "comparison_of_presented",
        }

    async def _run_property_turn(
        self,
        *,
        sid: str,
        utterance: str,
        t_start: float,
        timings: dict[str, float],
    ) -> dict[str, Any]:
        """Property (hotel-concierge) fast path — ONE render LLM call per turn.

        The full travel pipeline spends an LLM round-trip on classify+decompose
        before the render ever starts. That buys region/hotel extraction and
        source routing — all meaningless when the bot IS one hotel. This path
        mirrors the legacy hotel brain's shape (retrieve guide chunks → answer)
        on the concierge machinery:

          - escalation classification runs CONCURRENTLY with session load +
            guide retrieval (it gates the render but adds ~0 wall time),
          - the render is the only blocking LLM call (persona rules make it
            answer in the guest's language, so skipping the decomposer's
            language detection costs nothing),
          - the deterministic micro-branches (web-offer yes/no, explicit
            "search online") are kept — they are regex checks, not LLM calls,
          - conversational turns ("thanks", "what did I ask?") are answered by
            the render from the transcript, exactly like the old hotel brain.
        """
        pid = self._property_hotel_id

        async def _classify_leg() -> dict[str, Any]:
            t0 = time.perf_counter()
            v = await self._classifier.classify(utterance)
            timings["classify_ms"] = _ms(time.perf_counter() - t0)
            return v

        async def _session_retrieve_leg() -> tuple[dict[str, Any], dict[str, Any]]:
            t0 = time.perf_counter()
            sess = await self._sessions.load(sid)
            timings["session_load_ms"] = _ms(time.perf_counter() - t0)
            # Pin the session to the property (scoped follow-ups, hybrid path).
            sess["active_hotel_id"] = pid
            t0 = time.perf_counter()
            retrieval_local = await self._run_kb(
                {"language": sess.get("language")}, sess, PATH_SCOPED
            )
            timings["retrieve_ms"] = _ms(time.perf_counter() - t0)
            return sess, retrieval_local

        verdict, (session, retrieval) = await asyncio.gather(
            _classify_leg(), _session_retrieve_leg()
        )

        if verdict.get("escalate"):
            # Actionable request → file a Telegram ticket for the hotel's
            # staff (the legacy create_ticket behaviour) and confirm it to
            # the guest. Falls back to the plain hand-off line when the
            # ticket layer is unavailable or delivery fails — the bot must
            # never claim staff were notified when they weren't.
            lang = (session.get("language") or "en").lower()
            ticket = None
            if self._property_ticketer is not None:
                t0 = time.perf_counter()
                try:
                    ticket = await self._property_ticketer.file_from_turn(
                        hotel_id=pid,
                        utterance=utterance,
                        transcript=build_transcript(session.get("history")),
                        language=session.get("language"),
                    )
                except Exception as e:  # noqa: BLE001 — never break the turn
                    logger.warning("[property-actions] ticketer failed: {}", e)
                timings["ticket_ms"] = _ms(time.perf_counter() - t0)
            if ticket:
                from voxtera.call_center.property_actions import ticket_filed_answer

                answer = ticket_filed_answer(lang, ticket["category"])
            else:
                answer = _escalation_answer(lang)
            await self._sessions.append_turn(
                session,
                utterance=utterance,
                decomposition=None,
                reason="property_ticket" if ticket else "escalation_classifier",
                answer=answer,
                is_clarification=False,
            )
            await self._sessions.save(session)
            return self._finish(
                sid=sid,
                utterance=utterance,
                path=PATH_ESCALATE,
                reason="property_ticket" if ticket else "escalation_classifier",
                escalation={**verdict, "ticket": ticket},
                answer=answer,
                t_start=t_start,
                timings=timings,
            )

        # Web-offer follow-ups and explicit "search online" requests reuse the
        # standard handlers (the hybrid path keeps the property scope).
        pending_web_offer = bool(session.pop("pending_web_offer", False))
        if self._web is not None and pending_web_offer and _is_affirmation(utterance):
            return await self._handle_web_request(sid, utterance, session, t_start, timings)
        if pending_web_offer and _is_refusal(utterance):
            lang = (session.get("language") or "en").lower()
            ack = (
                "Tabii, başka bir konuda yardımcı olabilir miyim?"
                if lang == "tr"
                else "No problem. Is there anything else I can help with?"
            )
            await self._sessions.append_turn(
                session,
                utterance=utterance,
                decomposition=None,
                reason="web_offer_declined",
                answer=ack,
                is_clarification=False,
            )
            await self._sessions.save(session)
            return self._finish(
                sid=sid,
                utterance=utterance,
                path="clarify",
                reason="web_offer_declined",
                answer=ack,
                t_start=t_start,
                timings=timings,
            )
        if self._web is not None and _is_web_search_request(utterance):
            return await self._handle_web_request(sid, utterance, session, t_start, timings)

        session["last_question"] = utterance

        # Minimal synthetic decomposition: the render formats language/type
        # from it; persona rules handle the actual reply language.
        decomposition = {"query_type": "scoped", "language": session.get("language")}
        answer = await self._render(utterance, decomposition, retrieval, session)

        await self._sessions.append_turn(
            session,
            utterance=utterance,
            decomposition=None,
            reason="property_fast",
            answer=answer,
            is_clarification=False,
        )
        await self._sessions.save(session)
        return self._finish(
            sid=sid,
            utterance=utterance,
            path=PATH_SCOPED,
            reason="property_fast",
            decomposition=decomposition,
            router={
                "path": PATH_SCOPED,
                "sources": ["property_kb"],
                "reason": "property_fast",
                "needs": None,
            },
            retrieval=retrieval,
            answer=answer,
            t_start=t_start,
            timings=timings,
        )

    async def _run_kb(
        self,
        decomposition: dict[str, Any],
        session: dict[str, Any],
        path: str,
    ) -> dict[str, Any]:
        """Dispatch KB retrieval: property guide (hotel-scoped requests) or
        CompoundAndDiscovery for scoped / broad Qdrant retrieval."""
        # Property scope (P1.4): every KB lookup — scoped, broad, the hotel
        # side of hybrid — reads this property's guide. The raw utterance is
        # the query (guide chunks match question phrasing better than the
        # decomposer's requirement keywords).
        if self._property_hotel_id:
            if self._property_kb is None:
                return {
                    "reason": "no_property_kb_configured",
                    "hotels": [],
                    "missing_requirements": [],
                }
            return await self._property_kb.retrieve(
                hotel_id=self._property_hotel_id,
                query=getattr(self, "_turn_utterance", "") or "",
                language=decomposition.get("language") or session.get("language"),
            )
        if self._compound is None:
            return {
                "reason": "no_retriever_configured",
                "hotels": [],
                "missing_requirements": [],
            }
        # If the user explicitly selected "All regions" (empty string),
        # ignore the decomposer's inferred region and search globally.
        if getattr(self, "_user_region_override", None) is not None:
            region = self._user_region_override  # "" means all regions
        else:
            region = decomposition.get("region") or session.get("active_region") or ""
        requirements = list(decomposition.get("requirements") or [])

        # ── Fine-grained / non-canonical geography ──────────────────────────
        # The corpus has ONE coarse region bucket ("Turkish Riviera"), so a
        # city/district like "Kaş" or a fancy region name like "Lycia" can
        # never be FILTERED — but the place IS written inside the chunks
        # ("…located in the Kaş district of Antalya"). Therefore:
        #   1. a region label Qdrant doesn't store must NOT become a filter
        #      (it would zero out every result) — drop it to all-regions;
        #   2. the place joins the SEMANTIC query as an extra requirement, so
        #      the AND-search pins results to hotels whose text mentions it.
        # This is what makes "what hotels are in Kaş?" actually find the Kaş
        # hotels the admin semantic-search panel shows.
        if not (
            path == PATH_SCOPED
            and (decomposition.get("hotel_id") or session.get("active_hotel_id"))
        ):
            known_regions = set(REGION_ALIASES.values())
            non_canonical_region = ""
            if region and canonical_region(region) not in known_regions:
                non_canonical_region = region
                region = ""  # don't filter on a label the payloads don't carry
            place = (
                (decomposition.get("district") or "").strip()
                or (decomposition.get("city") or "").strip()
                or non_canonical_region
            )
            if place and place.lower() not in " ".join(requirements).lower():
                requirements = requirements + [place]
        # Source the resolved hotel id from EITHER the decomposition (set by the
        # inline resolver on the PATH_HOTEL_RESOLVE branch) OR the session
        # (set on a prior turn; router then returns PATH_SCOPED directly with
        # reason "hotel_resolved"). Reading only `decomposition` silently drops
        # the scope on the session-resolved path, degrading a scoped lookup to a
        # generic broad search and returning the wrong hotel. See _run_kb scope
        # filter below.
        hotel_id = (
            (decomposition.get("hotel_id") or session.get("active_hotel_id"))
            if path == PATH_SCOPED
            else None
        )
        # Scoped query about a known hotel but no specific requirement → fall
        # back to a generic overview instead of failing closed (empty_requirements).
        if path == PATH_SCOPED and hotel_id and not requirements:
            requirements = list(_SCOPED_DEFAULT_REQUIREMENTS)
            logger.info(
                "scoped query for hotel_id={!r} had no requirements — injecting overview default",
                hotel_id,
            )
        if path == PATH_SCOPED and not hotel_id:
            # Scoped path reached without a resolved hotel id — the query will
            # degrade to a generic broad search over `requirements`. Surface it
            # rather than silently answering about the wrong hotel.
            logger.warning(
                "scoped path with unresolved hotel_id (mention={!r}) — "
                "retrieval will not be hotel-scoped",
                decomposition.get("hotel_mention"),
            )
        self._trace_qdrant("retrieve", requirements, region=region, hotel_id=hotel_id, rerank=True)
        # Resolved hotel: gather SEVERAL of its passages, not the single best
        # chunk. A question about a known hotel ("how far from the beach?") needs
        # the full picture — the address chunk AND the "located on the beach"
        # chunk — so the LLM can answer instead of seeing one chunk that happens
        # to miss the detail. cross-hotel discovery keeps one chunk per hotel by
        # design; for a single known hotel that is too thin.
        if path == PATH_SCOPED and hotel_id and hasattr(self._compound, "fetch_hotel_chunks"):
            chunks = await self._compound.fetch_hotel_chunks(
                hotel_id=hotel_id, query=" ".join(requirements), region=region, k=6
            )
            if chunks:
                evidence: dict[str, Any] = {}
                for i, c in enumerate(chunks):
                    label = c.get("category") or f"passage_{i + 1}"
                    if label in evidence:  # duplicate category → keep both
                        label = f"{label}_{i + 1}"
                    evidence[label] = c
                hotel = {
                    "hotel_id": hotel_id,
                    "score": float(chunks[0].get("score", 0.0)),
                    "payload": {
                        "hotel_name": chunks[0].get("hotel_name", ""),
                        "district": chunks[0].get("district", ""),
                        "region": chunks[0].get("region", ""),
                    },
                    "evidence": evidence,
                }
                return {
                    "region": region,
                    "requirements": requirements,
                    "normalized_requirements": requirements,
                    "top_score": float(chunks[0].get("score", 0.0)),
                    "count": 1,
                    "hotels": [hotel],
                    "missing_requirements": [],
                    "reason": "hotel_resolved",
                }
            # No chunks for this hotel → fall through to the single-chunk path.
        result = await self._compound.discover(
            region=region,
            requirements=requirements,
            activity_tags=None,
            category_hint=None,
            hotel_id=hotel_id,
        )
        # For scoped queries, filter results to the resolved hotel only.
        if hotel_id and path == PATH_SCOPED:
            result["hotels"] = [
                h for h in result.get("hotels", []) if h.get("hotel_id") == hotel_id
            ]
        return result

    async def _handle_web_request(
        self,
        sid: str,
        utterance: str,
        session: dict[str, Any],
        t_start: float,
        timings: dict[str, float],
    ) -> dict[str, Any]:
        """Handle an explicit 'search the web' request by re-running the previous
        question online (hybrid when a hotel is active)."""
        lang = (session.get("language") or "en").lower()
        prior = session.get("last_question")
        if not prior or self._web is None:
            answer = _ask_what_to_search(lang)
            session["pending_slots"] = ["web_query"]
            await self._sessions.append_turn(
                session,
                utterance=utterance,
                decomposition=None,
                reason="web_request_no_prior",
                answer=answer,
                is_clarification=True,
            )
            await self._sessions.save(session)
            return self._finish(
                sid=sid,
                utterance=utterance,
                path="clarify",
                reason="web_request_no_prior",
                answer=answer,
                clarification={"question": answer, "slot": "web_query", "language": lang},
                t_start=t_start,
                timings=timings,
            )

        # Resolve the hotel FIRST so the web query carries its real name +
        # location (the prior question may use a pronoun — "is there a spa
        # there?" — which is useless to a web search without the hotel name).
        active_hotel = session.get("active_hotel_id")
        kb: dict[str, Any] | None = None
        hotel_name = location = None
        decomp = {
            "hotel_id": active_hotel,
            "query_type": "scoped",
            "region": session.get("active_region"),
            "requirements": [prior],
        }
        if active_hotel:
            t0 = time.perf_counter()
            kb = await self._run_kb(decomp, session, PATH_SCOPED)
            timings["retrieve_ms"] = _ms(time.perf_counter() - t0)
            hotel_name, location = self._hotel_web_context(kb, decomp, session)
        t0 = time.perf_counter()
        dq = await self._web_query_from_dialog(prior, session)
        rev = _REVIEW_DOMAINS if _is_review_query(decomp, prior) else None
        web = await self._web.discover(
            prior,
            decomp,
            hotel_name=hotel_name,
            location=location,
            query=dq,
            include_domains=rev,
        )
        timings["web_ms"] = _ms(time.perf_counter() - t0)

        # The user explicitly said "yes, search online" — so just ANSWER the
        # question from the web. Do NOT re-render the hotel KB, which would
        # repeat the very "I don't have that — want me to check online?" offer
        # the user is responding to. The hotel context was already given last
        # turn. We still keep KB hotels for the evidence card, not the answer.
        synth = await self._synth_web(prior, web, lang, session=session)
        answer = _web_answer(web, lang, synth=synth)
        if active_hotel:
            retrieval = {
                "hotels": (kb or {}).get("hotels") or [],
                "web": web,
                "reason": "web_request",
            }
            path = PATH_HYBRID
        else:
            retrieval = {"web": web, "reason": "web_request"}
            path = PATH_WEB

        await self._sessions.append_turn(
            session,
            utterance=utterance,
            decomposition=None,
            reason="web_request",
            answer=answer,
            is_clarification=False,
        )
        await self._sessions.save(session)
        return self._finish(
            sid=sid,
            utterance=utterance,
            path=path,
            reason="web_request",
            retrieval=retrieval,
            answer=answer,
            t_start=t_start,
            timings=timings,
        )

    async def _web_query_from_dialog(self, utterance: str, session: dict[str, Any]) -> str | None:
        """Rewrite the conversation into ONE web search query via the LLM.

        Uses the full transcript (which already holds the resolved hotel name and
        location from earlier bot answers), so the query doesn't depend on the ES
        resolver or the decomposition. Returns None when no fn is wired or it
        errors — the WebRetriever then falls back to its heuristic builder.
        """
        if self._web_query_fn is None:
            return None
        try:
            # D19 — region coherence: the rewrite must anchor "near the hotel"
            # questions to the ACTIVE hotel's real location, not to whichever
            # city dominates the transcript (a Göynük hotel was getting
            # Istanbul restaurant suggestions because the chat began there).
            anchor = ""
            if session.get("active_hotel_id") and session.get("active_hotel_location"):
                anchor = (
                    f"ACTIVE HOTEL LOCATION: the guest's current hotel is in "
                    f"{session['active_hotel_location']} — anchor any 'near the "
                    f"hotel / nearby' search THERE, even if other cities were "
                    f"discussed earlier."
                )
            result = self._web_query_fn(
                {
                    "utterance": utterance,
                    "transcript": build_transcript(session.get("history")),
                    "language": (session.get("language") or "en"),
                    "anchor": anchor,
                }
            )
            q = (await result) if hasattr(result, "__await__") else result
            return (q or "").strip() or None
        except Exception as e:  # noqa: BLE001 — never fail the turn over query rewrite
            logger.warning("web query rewrite failed: {}", e)
            return None

    async def _synth_web(
        self,
        question: str,
        web: dict[str, Any],
        language: str,
        hotel_facts: str | None = None,
        session: dict[str, Any] | None = None,
    ) -> str | None:
        """Rewrite a raw web-search result into ONE clean spoken answer.

        Tavily's `answer` often merges several sources into a self-contradictory
        blob ("on the beach ... a 13-minute walk ... directly on the beach").
        The injected synth LLM is asked to resolve that into a single coherent
        reply for the voice channel. When `hotel_facts` is given (hybrid path),
        the synth also weaves in what the hotel's own guide says — producing ONE
        answer instead of a hotel reply glued to a web reply. Returns None (caller
        falls back) when no synth fn is wired or it errors.
        """
        if self._web_synth_fn is None or not (web or {}).get("answer"):
            return None
        try:
            result = self._web_synth_fn(
                {
                    "question": question,
                    "web": web,
                    "language": language,
                    "hotel_facts": hotel_facts,
                    # Recent dialogue so the synth varies its openings instead of
                    # parroting "Great question —" every turn.
                    "transcript": build_transcript(
                        (session or {}).get("history"), char_budget=1500
                    ),
                }
            )
            return (await result) if hasattr(result, "__await__") else result
        except Exception as e:  # noqa: BLE001 — never fail the turn over synthesis
            logger.warning("web synthesis failed: {}", e)
            return None

    @staticmethod
    def _hotel_web_context(
        kb: dict[str, Any] | None,
        decomposition: dict[str, Any],
        session: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """Resolve (hotel_name, location) for a hotel-scoped web query.

        Trust the KB hotel only if it IS the active/subject hotel — a hybrid KB
        leg can return generic hotels, whose name/location would mislead the web
        search. Otherwise fall back to the decomposer's spoken mention + geo.
        """
        active = session.get("active_hotel_id")
        name: str | None = None
        location: str | None = None
        for h in (kb or {}).get("hotels") or []:
            if not active or h.get("hotel_id") == active:
                p = h.get("payload") or {}
                name = p.get("hotel_name") or None
                location = p.get("district") or p.get("region") or None
                break
        name = name or decomposition.get("hotel_mention")
        location = (
            location
            # D19: the active hotel's REAL location beats conversation
            # geography — "restaurants near the hotel" must follow the hotel.
            or session.get("active_hotel_location")
            or decomposition.get("city")
            or decomposition.get("region")
            or session.get("active_region")
        )
        return name, location

    async def _handle_converse(
        self,
        sid: str,
        utterance: str,
        session: dict[str, Any],
        decomposition: dict[str, Any],
        t_start: float,
        timings: dict[str, float],
    ) -> dict[str, Any]:
        """Answer a conversational/meta/recall turn from the transcript — no
        retrieval. This is what makes the agent a dialogue partner instead of a
        question-answering machine: it can greet, acknowledge, recall what was
        asked, and summarise the conversation."""
        lang = (decomposition.get("language") or session.get("language") or "en").lower()
        transcript = build_transcript(session.get("history"))
        answer = None
        if self._converse_fn is not None:
            try:
                result = self._converse_fn(
                    {"utterance": utterance, "transcript": transcript, "language": lang}
                )
                answer = (await result) if hasattr(result, "__await__") else result
            except Exception as e:  # noqa: BLE001
                logger.warning("converse failed: {}", e)
        if not answer:
            answer = (
                "Tabii, size nasıl yardımcı olabilirim?"
                if lang == "tr"
                else "Sure — how can I help you with your hotel search?"
            )
        await self._sessions.append_turn(
            session,
            utterance=utterance,
            decomposition=decomposition,
            reason="conversational",
            answer=answer,
            is_clarification=False,
        )
        await self._sessions.save(session)
        return self._finish(
            sid=sid,
            utterance=utterance,
            path="conversational",
            reason="conversational",
            decomposition=decomposition,
            answer=answer,
            t_start=t_start,
            timings=timings,
        )

    def _trace_qdrant(self, stage: str, query: Any, **kw: Any) -> None:
        q = query if isinstance(query, str) else ", ".join(str(x) for x in (query or []))
        entry = {"store": "qdrant", "stage": stage, "query": q}
        entry.update({k: v for k, v in kw.items() if v is not None or k == "rerank"})
        getattr(self, "_trace", {"qdrant": []}).setdefault("qdrant", []).append(entry)

    def _trace_es(self, stage: str, query: str, **kw: Any) -> None:
        entry = {"store": "elasticsearch", "stage": stage, "query": query}
        entry.update(kw)
        getattr(self, "_trace", {"es": []}).setdefault("es", []).append(entry)

    async def _detect_named_hotel(self, utterance: str) -> str | None:
        """Vector-store named-hotel detection (LLM-independent).

        Searches the cleaned utterance in Qdrant and locks onto a single
        DOMINANT hotel. The embedding treats a hotel name as a whole concept
        ("Casa Dell Arte") and isn't fooled by noise tokens like "beach" that
        BM25 matches against the wrong "...Beach Resort" — so this is more robust
        than ES for a name buried in a sentence. A generic query ("spa hotel")
        yields a tight cluster, not a peak, so nothing is detected. ES (the
        resolver) is still used for clean, explicitly-extracted mentions.
        """
        if self._compound is None:
            return None
        # Use the FULL utterance — the embedding handles the whole sentence, and
        # the admin semantic-search panel confirms the full query ranks the right
        # hotel top. Cleaning/stripping words shifts the embedding and flips the
        # ranking to a different same-name hotel, so we do NOT clean here.
        q = (utterance or "").strip()
        if not q:
            return None
        try:
            # rerank=False: the cross-encoder over-weights common tokens ("beach")
            # for name lookups; the RAW vector order is what correctly ranks
            # "Casa Dell Arte" top (matches the admin semantic-search panel).
            res = await self._compound.discover(region="", requirements=[q], rerank=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("hotel detection (vector) failed: {}", e)
            self._trace_qdrant("detect", q, region="(all)", rerank=False, resolved="(error)")
            return None
        hotels = (res or {}).get("hotels") or []
        if not hotels:
            self._trace_qdrant("detect", q, region="(all)", rerank=False, resolved="(no hits)")
            return None
        top = hotels[0]
        ts = float(top.get("score") or 0.0)
        second = float(hotels[1].get("score") or 0.0) if len(hotels) > 1 else 0.0
        # Confident single named hotel: strong absolute score AND a clear gap to
        # the runner-up. A broad query returns several near-tied hotels → no peak.
        # Accept when either (a) the top score is strong in absolute terms AND
        # keeps at least a small lead — a confident name match — or (b) it clears
        # the floor with a clear margin. (a) rescues near-tied same-name hotels
        # that (b) would reject, while still refusing a generic near-PERFECT tie
        # ("do they have spa?" → 0.827 vs 0.826) that would hijack the active hotel.
        score_ok = (
            ts >= _DETECT_STRONG_SCORE
            and (len(hotels) == 1 or ts - second >= _DETECT_STRONG_MARGIN)
        ) or (ts >= _DETECT_MIN_SCORE and (len(hotels) == 1 or ts - second >= _DETECT_MARGIN))
        # The detected hotel's NAME must actually appear in the utterance — else a
        # high score is just a content match (a "restaurants" query matching a
        # restaurant-dense hotel) and would wrongly hijack the active hotel.
        hotel_name = (top.get("payload") or {}).get("hotel_name") or ""
        name_ok = _detected_name_in_utterance(hotel_name, utterance)
        dominant = score_ok and name_ok
        # Record what detection saw — top hotel, scores, and WHY it was accepted or
        # rejected (score gate vs name-in-utterance check) — for the debug panel.
        if not score_ok:
            verdict = "rejected: weak/tied score"
        elif not name_ok:
            verdict = f"rejected: name {hotel_name!r} not in utterance"
        else:
            verdict = "accepted"
        self._trace_qdrant(
            "detect",
            q,
            region="(all)",
            rerank=False,
            resolved=f"{top.get('hotel_id')} ({ts:.3f}, 2nd={second:.3f}, {verdict})",
        )
        if dominant:
            logger.info(
                "detected named hotel {!r} via vector store (score={:.3f}) — LLM missed it",
                top.get("hotel_id"),
                ts,
            )
            return top.get("hotel_id")
        return None

    async def _semantic_fallback(
        self,
        utterance: str,
        decomposition: dict[str, Any],
        session: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Search the raw utterance when the structured path returned nothing.

        Uses the same CompoundAndDiscovery but with the FULL utterance as the
        single requirement, so the hotel name / full context survives. Returns
        the result tagged ``reason="semantic_fallback"`` if it found any hotels,
        else None. Region scope is inherited (empty == all regions).
        """
        if self._compound is None:
            return None
        q = (utterance or "").strip()
        if not q:
            return None
        region = decomposition.get("region") or session.get("active_region") or ""
        self._trace_qdrant("fallback", q, region=region, rerank=True)
        try:
            result = await self._compound.discover(region=region, requirements=[q])
        except Exception as e:  # noqa: BLE001
            logger.warning("semantic fallback failed: {}", e)
            return None
        if (result or {}).get("hotels"):
            result["reason"] = "semantic_fallback"
            logger.info(
                "semantic fallback recovered {} hotel(s) for {!r}",
                len(result["hotels"]),
                q,
            )
            return result
        return None

    async def _render(
        self,
        utterance: str,
        decomposition: dict[str, Any],
        retrieval: dict[str, Any] | None,
        session: dict[str, Any],
    ) -> str:
        """Call the injected render_fn, or return a defensive fallback.

        Fails closed when retrieval produced zero hotels — the LLM has
        no evidence to ground on and tends to invent geography ("scoped
        to Paris") if asked to generate prose anyway.
        """
        hotels = (retrieval or {}).get("hotels") or []
        if not hotels:
            lang = (decomposition.get("language") or session.get("language") or "en").lower()
            # Respect the user's "All regions" override for the no-match message.
            if getattr(self, "_user_region_override", None) is not None:
                region = self._user_region_override
            else:
                region = (
                    decomposition.get("region")
                    or decomposition.get("city")
                    or session.get("active_region")
                    or ""
                )
            return _no_match_answer(lang, region)
        if self._render_fn is None:
            names = ", ".join(
                (h.get("payload") or {}).get("hotel_name", h.get("hotel_id")) for h in hotels[:3]
            )
            return f"Top matches: {names}."
        try:
            return await self._render_fn(
                {
                    "utterance": utterance,
                    "region": decomposition.get("region") or session.get("active_region"),
                    "decomposition": decomposition,
                    "retrieval": retrieval or {},
                    "transcript": build_transcript(session.get("history")),
                    "brief": getattr(self, "_brief", False),
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ConciergePipeline render failed: {}", e)
            return "Sorry, I had trouble forming a reply."

    def _finish(
        self,
        *,
        sid: str,
        utterance: str,
        path: str,
        reason: str,
        t_start: float,
        timings: dict[str, float],
        decomposition: dict[str, Any] | None = None,
        router: dict[str, Any] | None = None,
        retrieval: dict[str, Any] | None = None,
        escalation: dict[str, Any] | None = None,
        clarification: dict[str, Any] | None = None,
        answer: str | None = None,
    ) -> dict[str, Any]:
        timings["total_ms"] = _ms(time.perf_counter() - t_start)
        result = {
            "session_id": sid,
            "utterance": utterance,
            "path": path,
            "reason": reason,
            "escalation": escalation,
            "clarification": clarification,
            "decomposition": decomposition,
            "router": router,
            "retrieval": retrieval,
            "answer": answer,
            "timings": timings,
            "trace": getattr(self, "_trace", {"qdrant": [], "es": []}),
        }
        self._log_query(result)
        return result

    def _log_query(self, result: dict[str, Any]) -> None:
        """Append a structured NDJSON record to the concierge query log."""
        try:
            log_file = concierge_log_file()
            # Full debug record: keep everything needed to replay a turn from the
            # log alone — the dialog, the decomposition, the store trace (what was
            # sent to Qdrant/ES/web and what came back), and timings. Hotel
            # evidence is kept as ids+scores+names (full chunk text dropped to
            # keep lines manageable; it's reproducible from the hotel_id).
            retrieval = result.get("retrieval") or {}
            hotels_summary = [
                {
                    "hotel_id": h.get("hotel_id"),
                    "score": round(float(h.get("score", 0)), 3),
                    "name": (h.get("payload") or {}).get("hotel_name"),
                    "evidence_categories": list((h.get("evidence") or {}).keys()),
                }
                for h in retrieval.get("hotels", [])
            ]
            web = retrieval.get("web") or {}
            record = {
                "ts": datetime.now(UTC).isoformat(),
                "session_id": result.get("session_id"),
                "utterance": result.get("utterance"),
                "answer": result.get("answer"),
                "path": result.get("path"),
                "reason": result.get("reason"),
                "decomposition": result.get("decomposition"),
                "router": result.get("router"),
                "trace": result.get("trace"),
                "retrieval_summary": {
                    "hotels": hotels_summary,
                    "count": len(hotels_summary),
                    "region": retrieval.get("region"),
                    "missing_requirements": retrieval.get("missing_requirements", []),
                    "reason": retrieval.get("reason"),
                },
                "web": {
                    "query": web.get("query"),
                    "answer": web.get("answer"),
                    "sources": [s.get("url") for s in (web.get("sources") or [])],
                    "elapsed_ms": web.get("elapsed_ms"),
                }
                if web
                else None,
                "clarification": result.get("clarification"),
                "escalation": result.get("escalation"),
                "timings": result.get("timings"),
            }
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.debug("concierge log write failed: {}", e)


def concierge_log_file() -> Path:
    """Path to today's concierge NDJSON log (query + feedback records)."""
    log_dir = Path(os.environ.get("CONCIERGE_LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return log_dir / f"travel_agent_consierge-{today}.jsonl"


def append_feedback_record(record: dict[str, Any]) -> None:
    """Append a user thumbs-up/down feedback record to the concierge log.

    Query records have no ``type`` field; feedback records carry
    ``type: "feedback"`` so log consumers can tell them apart. Correlation
    back to the rated turn is via (session_id, utterance, answer).
    """
    try:
        line = {"type": "feedback", "ts": datetime.now(UTC).isoformat(), **record}
        with open(concierge_log_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.debug("concierge feedback write failed: {}", e)
