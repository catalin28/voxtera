"""Hotel KB Qdrant configuration constants.

Single source of truth for collection name, vector size, distance metric,
retrieval defaults, and the curated chunk-category enum used by all
Phase 2 retrievers and the ingestion pipeline.
"""

from __future__ import annotations

# Qdrant collection
QDRANT_COLLECTION = "hotel_kb"
EMBEDDING_DIM = 1024
DISTANCE = "Cosine"

# Retrieval defaults (overridable via env in callers).
# NOTE on DEFAULT_MIN_SCORE: multilingual-e5-large produces a highly compressed
# cosine range (~0.74 [CLS] floor → ~0.85 strong match). Absolute thresholding
# cannot separate signal from noise on its own; live calibration showed real
# matches at 0.77–0.82 and pure-junk queries at 0.76–0.77. We keep an absolute
# floor only to catch catastrophic failures (out-of-domain / embedding errors).
# Real relevance filtering uses RELATIVE_MARGIN (drop chunks whose score is more
# than RELATIVE_MARGIN below the top match for the query) + top-K + LLM-side
# check on chunk text. This sharpens compound-AND intersection precision.
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 0.70
RELATIVE_MARGIN = 0.05

# Compound-AND defaults (Phase 2c)
DEFAULT_MAX_REQUIREMENTS = 5

# Broad-discovery defaults (Phase 2b)
DEFAULT_MAX_HOTELS = 5
DISCOVERY_OVERSHOOT_MULT = 6

# Cross-encoder rerank defaults (Phase 3a)
# bge-reranker-v2-m3 is multilingual (matches multilingual-e5-large above).
# Raw model output is a logit; we apply sigmoid in the reranker so all callers
# see a [0,1] score. With sigmoid applied:
#   ~0.50 ≈ borderline · ~0.70 ≈ confident match · ~0.95+ ≈ exact match
# RERANK_MIN_SCORE drops hits below an absolute floor (real separation, unlike
# the e5 0.70 floor which is a junk-floor only). RERANK_RELATIVE_MARGIN trims
# any hit whose rerank score is more than this far below the top hit, also on
# the [0,1] scale.
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
RERANK_MIN_SCORE = 0.50
RERANK_RELATIVE_MARGIN = 0.15

# Curated chunk categories (architecture v0.3 §6 + dev plan Phase 2f)
CATEGORIES = (
    "overview",
    "rooms",
    "amenities",
    "food_beverage",
    "wellness",
    "policies",
    "children",
    "activities",
    "accessibility",
    "location",
    "atmosphere",
    "packages",
)


# Region aliases — map user-facing region tokens (city slugs from the
# demo dropdown, decomposer outputs) to the canonical region labels
# stored in Qdrant payloads. Today the corpus is one bucket
# ("Turkish Riviera"); as ingestion grows finer-grained these aliases
# can be removed.
REGION_ALIASES: dict[str, str] = {
    "antalya": "Turkish Riviera",
    "belek": "Turkish Riviera",
    "kemer": "Turkish Riviera",
    "side": "Turkish Riviera",
    "alanya": "Turkish Riviera",
    "bodrum": "Turkish Riviera",
    "turkish riviera": "Turkish Riviera",
}


def canonical_region(region: str | None) -> str:
    """Return the canonical region label as stored in Qdrant payloads."""
    if not region:
        return ""
    return REGION_ALIASES.get(region.strip().lower(), region.strip())
