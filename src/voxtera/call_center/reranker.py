"""Cross-encoder reranker for the call-center RAG stack (Phase 3a).

The first-stage retriever (`BroadHotelDiscovery`) issues a single
multilingual-e5-large vector search per requirement and returns up to
`DEFAULT_MAX_HOTELS * DISCOVERY_OVERSHOOT_MULT` hits. e5-large cosines
are highly compressed (`~0.74` junk floor → `~0.85` strong match) and
do not separate signal from noise on their own — calibration in Phase
2c showed real matches at `0.77–0.82` and pure-junk queries at
`0.76–0.77`.

The reranker re-scores the `(query, chunk_text)` pairs with a true
cross-encoder. We use ``BAAI/bge-reranker-v2-m3`` because it is
multilingual and matches the languages we already embed
(en/tr/ru/fr/de/es/…). Raw model output is a logit; we apply sigmoid
so all callers consume a normalised `[0, 1]` score. The sigmoid bands
are calibrated upstream in `kb_config.RERANK_MIN_SCORE` /
`RERANK_RELATIVE_MARGIN`.

The reranker is dependency-injectable: tests pass a deterministic
`rerank_fn` and never load the model. Production code lazy-loads the
model on first use.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Awaitable, Callable
from typing import Union

from loguru import logger

from voxtera.call_center.kb_config import RERANK_MODEL

# rerank_fn(query, passages) -> list[float] in [0, 1], one per passage.
# Sync return is allowed; the caller awaits coroutines and accepts lists.
RerankFn = Callable[[str, list[str]], Union[Awaitable[list[float]], list[float]]]

_RERANK_ENABLED_ENV = "RAG_RERANK_ENABLED"


def is_rerank_enabled() -> bool:
    """Read the kill-switch env var. Defaults to ON in production.

    Set ``RAG_RERANK_ENABLED=false`` (or 0/no/off) to disable, e.g. to
    A/B compare against the pre-rerank baseline or to fall back fast if
    the model misbehaves in production.
    """
    raw = (os.environ.get(_RERANK_ENABLED_ENV) or "true").strip().lower()
    return raw not in {"false", "0", "no", "off", ""}


def sigmoid(x: float) -> float:
    """Numerically-stable sigmoid for logit -> [0, 1] conversion."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class CrossEncoderReranker:
    """Lazy-loaded ``sentence_transformers.CrossEncoder`` wrapper.

    The model is loaded on first call so test runs that inject their
    own ``rerank_fn`` never pay the load cost (~1 GB download on the
    first run for bge-reranker-v2-m3).
    """

    def __init__(self, model_name: str = RERANK_MODEL) -> None:
        self._model_name = model_name
        self._model = None

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            logger.info("Loading cross-encoder reranker: {}", self._model_name)
            t0 = time.perf_counter()
            self._model = CrossEncoder(self._model_name)
            logger.info("Reranker loaded in {:.1f}s", time.perf_counter() - t0)
        return self._model

    def __call__(self, query: str, passages: list[str]) -> list[float]:
        """Score (query, passage) pairs -> list of [0, 1] floats.

        Returns an empty list if `passages` is empty. The model is
        called once with the full batch; sentence-transformers handles
        sub-batching internally.
        """
        if not passages:
            return []
        model = self._get_model()
        pairs = [(query, p or "") for p in passages]
        # CrossEncoder.predict returns numpy array of raw logits.
        logits = model.predict(pairs)
        return [sigmoid(float(x)) for x in logits]
