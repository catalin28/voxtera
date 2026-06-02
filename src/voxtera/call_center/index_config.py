"""Elasticsearch index configuration for the hotels resolver.

Centralizes the index name, brand keywords, synonyms, and full index mapping
so the admin server and resolver share a single source of truth.
"""

from __future__ import annotations

from typing import Any

ES_INDEX = "hotels"

# Brand tokens to protect from Turkish stemming (e.g. "Rixos" -> "rixo").
BRAND_KEYWORDS: list[str] = [
    "rixos", "maxx", "royal", "cornelia", "voyage", "gloria",
    "xanadu", "limak", "atlantis", "selectum", "regnum", "carya",
    "akra", "hilton", "sheraton", "marriott", "hyatt", "radisson",
    "kempinski", "fairmont", "dedeman", "wyndham",
    "crystal", "calista", "ela", "susesi", "titanic",
    "kaya", "palazzo", "bomonti", "premium", "belek", "lara",
]

# Query-time synonyms for common spoken / mistyped variants.
HOTEL_SYNONYMS: list[str] = [
    "max royal, maksi royal => maxx royal",
    "riksos => rixos",
    "kornelia => cornelia",
    "regnum karya => regnum carya",
    "selektum => selectum",
]


def build_hotel_mapping() -> dict[str, Any]:
    """Return the Elasticsearch index settings + mappings for the hotels index."""
    return {
        "settings": {
            "analysis": {
                "filter": {
                    "turkish_stop": {"type": "stop", "stopwords": "_turkish_"},
                    "turkish_stemmer": {"type": "stemmer", "language": "turkish"},
                    "brand_keywords": {
                        "type": "keyword_marker",
                        "keywords": BRAND_KEYWORDS,
                    },
                    "hotel_synonyms": {
                        "type": "synonym_graph",
                        "synonyms": HOTEL_SYNONYMS,
                    },
                },
                "analyzer": {
                    "turkish_custom": {
                        "type": "custom",
                        "tokenizer": "standard",
                        "filter": [
                            "lowercase",
                            "brand_keywords",
                            "turkish_stop",
                            "turkish_stemmer",
                        ],
                    },
                    "turkish_search": {
                        "type": "custom",
                        "tokenizer": "standard",
                        "filter": [
                            "lowercase",
                            "hotel_synonyms",
                            "brand_keywords",
                            "turkish_stop",
                            "turkish_stemmer",
                        ],
                    },
                },
            }
        },
        "mappings": {
            "properties": {
                "hotel_id": {"type": "keyword"},
                "name": {
                    "type": "text",
                    "analyzer": "turkish_custom",
                    "search_analyzer": "turkish_search",
                    "fields": {"raw": {"type": "keyword"}},
                },
                "aliases": {
                    "type": "text",
                    "analyzer": "turkish_custom",
                    "search_analyzer": "turkish_search",
                },
                "chain": {"type": "keyword"},
                "city": {"type": "keyword"},
                "district": {"type": "keyword"},
                "region": {"type": "keyword"},
                "country": {"type": "keyword"},
                "price_tier": {"type": "keyword"},
                "coordinates": {"type": "geo_point"},
                "activity_tags": {"type": "keyword"},
                "beachfront": {"type": "boolean"},
                "adults_only": {"type": "boolean"},
                "board_type": {"type": "keyword"},
                "star_rating": {"type": "integer"},
            }
        },
    }
