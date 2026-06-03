"""Hotel mention resolver for Phase 1 call-center workflow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

import aiohttp
from loguru import logger

from voxtera.call_center.clients import es_request

SearchFn = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


@dataclass(slots=True)
class ResolverCandidate:
    """Normalized candidate returned by Elasticsearch."""

    hotel_id: str
    name: str
    score: float


@dataclass(slots=True)
class ResolverResult:
    """Stable resolver output contract used by downstream phases."""

    decision: str
    hotel_id: str | None
    top_score: float
    candidates: list[ResolverCandidate]
    reason: str
    normalized_mention: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidates"] = [asdict(c) for c in self.candidates]
        return payload


class HotelResolver:
    """Resolve a caller hotel mention into a canonical hotel id.

    Decision policy:
    - score >= 0.85: auto_resolve
    - 0.55 <= score < 0.85: needs_clarification (top 3)
    - score < 0.55: no_match
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        index_name: str = "hotels",
        auto_threshold: float = 0.85,
        clarify_threshold: float = 0.55,
        max_candidates: int = 3,
        search_fn: SearchFn | None = None,
        score_norm: float = 100.0,
    ) -> None:
        self._session = session
        self._index_name = index_name
        self._auto_threshold = auto_threshold
        self._clarify_threshold = clarify_threshold
        self._max_candidates = max_candidates
        self._search_fn = search_fn
        self._score_norm = score_norm

    async def resolve(self, mention_text: str) -> dict[str, Any]:
        """Resolve *mention_text* and return a stable decision payload."""
        normalized = self._normalize_mention(mention_text)
        if not normalized:
            return self._result(
                decision="no_match",
                hotel_id=None,
                top_score=0.0,
                candidates=[],
                reason="empty_mention",
                normalized_mention=normalized,
            )

        try:
            hits = await self._search(normalized, size=10)
            candidates = self._parse_hits(hits)
            return self._decide(candidates, normalized)
        except Exception as exc:  # pragma: no cover - defensive runtime path
            logger.exception("Resolver search failed for mention '{}': {}", mention_text, exc)
            return self._result(
                decision="no_match",
                hotel_id=None,
                top_score=0.0,
                candidates=[],
                reason="resolver_error",
                normalized_mention=normalized,
            )

    def _normalize_mention(self, text: str) -> str:
        normalized = text.strip().lower()
        normalized = normalized.replace("`", "'").replace("\u2019", "'").replace("\u02bc", "'")
        normalized = " ".join(normalized.split())
        return normalized

    async def _search(self, query: str, *, size: int) -> list[dict[str, Any]]:
        if self._search_fn is not None:
            return await self._search_fn(query, size)

        if self._session is None:
            raise RuntimeError("HotelResolver requires a session or a custom search_fn")

        response = await es_request(
            self._session,
            "POST",
            f"/{self._index_name}/_search",
            json={"size": size, "query": self._build_query(query)},
        )
        hits = response.get("hits", {}).get("hits", [])

        # Normalize raw ES BM25 scores to [0, 1] using a reference constant.
        # A perfect exact name match (boost 10) scores ~95-100; we divide by 100
        # so thresholds (0.85 auto, 0.55 clarify) work correctly.
        for hit in hits:
            raw = float(hit.get("_score", 0.0))
            hit["_score"] = min(raw / self._score_norm, 1.0)

        return hits

    def _build_query(self, query: str) -> dict[str, Any]:
        # Field boosts reflect identity strength: name > aliases > chain > location.
        return {
            "bool": {
                "should": [
                    {
                        "match": {
                            "name": {
                                "query": query,
                                "boost": 10,
                                "fuzziness": "AUTO",
                            }
                        }
                    },
                    {
                        "match": {
                            "aliases": {
                                "query": query,
                                "boost": 8,
                                "fuzziness": "AUTO",
                            }
                        }
                    },
                    {"match": {"chain": {"query": query, "boost": 5}}},
                    {"match": {"district": {"query": query, "boost": 2}}},
                    {"match": {"city": {"query": query, "boost": 2}}},
                ],
                "minimum_should_match": 1,
            }
        }

    def _parse_hits(self, hits: list[dict[str, Any]]) -> list[ResolverCandidate]:
        candidates: list[ResolverCandidate] = []
        for hit in hits:
            source = hit.get("_source", {})
            hotel_id = source.get("hotel_id")
            name = source.get("name", "")
            if not isinstance(hotel_id, str) or not hotel_id:
                continue
            if not isinstance(name, str):
                continue

            score = float(hit.get("_score", 0.0))
            candidates.append(ResolverCandidate(hotel_id=hotel_id, name=name, score=score))

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    def _decide(
        self, candidates: list[ResolverCandidate], normalized_mention: str
    ) -> dict[str, Any]:
        if not candidates:
            return self._result(
                decision="no_match",
                hotel_id=None,
                top_score=0.0,
                candidates=[],
                reason="no_candidates",
                normalized_mention=normalized_mention,
            )

        top = candidates[0]
        if top.score >= self._auto_threshold:
            return self._result(
                decision="auto_resolve",
                hotel_id=top.hotel_id,
                top_score=top.score,
                candidates=[],
                reason="score_above_auto_threshold",
                normalized_mention=normalized_mention,
            )

        if top.score >= self._clarify_threshold:
            return self._result(
                decision="needs_clarification",
                hotel_id=None,
                top_score=top.score,
                candidates=candidates[: self._max_candidates],
                reason="score_in_clarification_band",
                normalized_mention=normalized_mention,
            )

        return self._result(
            decision="no_match",
            hotel_id=None,
            top_score=top.score,
            candidates=[],
            reason="score_below_min_threshold",
            normalized_mention=normalized_mention,
        )

    def _result(
        self,
        *,
        decision: str,
        hotel_id: str | None,
        top_score: float,
        candidates: list[ResolverCandidate],
        reason: str,
        normalized_mention: str,
    ) -> dict[str, Any]:
        result = ResolverResult(
            decision=decision,
            hotel_id=hotel_id,
            top_score=top_score,
            candidates=candidates,
            reason=reason,
            normalized_mention=normalized_mention,
        )
        return result.to_dict()
