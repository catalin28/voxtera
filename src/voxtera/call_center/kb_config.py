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
# floor only to catch catastrophic failures (out-of-domain / embedding errors);
# real relevance filtering happens via top-K + LLM-side check on chunk text.
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 0.70

# Broad-discovery defaults (Phase 2b)
DEFAULT_MAX_HOTELS = 5
DISCOVERY_OVERSHOOT_MULT = 6

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
