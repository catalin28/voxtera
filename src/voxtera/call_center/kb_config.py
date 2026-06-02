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

# Retrieval defaults (overridable via env in callers)
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 0.25

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
