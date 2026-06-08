# Elasticsearch Optimisation — Voxtera Hotel Resolver

## Current Setup

- **Elasticsearch 8.19.16** on Ubuntu 24.04, single-node, 512 MB heap
- **Index**: `hotels` with `turkish_custom` analyzer (standard tokenizer → lowercase → turkish_stop → turkish_stemmer)
- **Known issue**: Turkish stemmer over-stems brand names (e.g. "Rixos" → "rixo", "Hilton" → "hilto")

---

## 1. Keyword Marker Filter (Priority: High)

**Problem**: The Turkish stemmer aggressively stems hotel brand names, breaking exact-match recall.

**Solution**: Add a `keyword_marker` token filter that protects known brand tokens from stemming.

```json
{
  "settings": {
    "analysis": {
      "filter": {
        "brand_keywords": {
          "type": "keyword_marker",
          "keywords": [
            "rixos", "maxx", "royal", "cornelia", "voyage", "gloria",
            "xanadu", "limak", "atlantis", "selectum", "regnum", "carya",
            "akra", "hilton", "sheraton", "marriott", "hyatt", "radisson",
            "kempinski", "swissôtel", "fairmont", "dedeman", "wyndham",
            "crystal", "tat", "calista", "ela", "susesi", "titanic"
          ]
        }
      },
      "analyzer": {
        "turkish_custom": {
          "type": "custom",
          "tokenizer": "standard",
          "filter": [
            "lowercase",
            "brand_keywords",
            "turkish_stop",
            "turkish_stemmer"
          ]
        }
      }
    }
  }
}
```

**Effect**: Tokens matching brand names pass through the stemmer untouched, preserving exact surface forms.

---

## 2. Synonym Filter (Priority: High)

**Problem**: Callers use colloquial names, abbreviations, or alternate spellings that don't match indexed text.

**Solution**: A synonym filter applied at query time expands caller input to canonical forms.

```json
{
  "filter": {
    "hotel_synonyms": {
      "type": "synonym",
      "synonyms": [
        "maxx royal, max royal, maksi royal => maxx royal",
        "rixos premium, rixos => rixos",
        "gloria sports, gloria spor => gloria sports arena",
        "limak, limak atlantis => limak atlantis",
        "cornelia, kornelia => cornelia de luxe",
        "regnum, regnum carya => regnum carya",
        "selectum, selektum => selectum luxury"
      ]
    }
  }
}
```

**Placement**: Insert `hotel_synonyms` in the analyzer filter chain *before* stemming, or use a separate `search_analyzer` with synonyms to keep the index analyzer clean.

---

## 3. Phonetic Analyzer (Priority: Medium)

**Problem**: Callers mispronounce hotel names or the STT transcribes them phonetically (e.g. "Rıksos", "Kornelya").

**Solution**: Add a phonetic sub-field using the `analysis-phonetic` plugin with Beider-Morse or Double Metaphone.

```json
{
  "mappings": {
    "properties": {
      "name": {
        "type": "text",
        "analyzer": "turkish_custom",
        "fields": {
          "phonetic": {
            "type": "text",
            "analyzer": "phonetic_analyzer"
          }
        }
      }
    }
  }
}
```

```json
{
  "settings": {
    "analysis": {
      "filter": {
        "double_metaphone": {
          "type": "phonetic",
          "encoder": "double_metaphone",
          "replace": false
        }
      },
      "analyzer": {
        "phonetic_analyzer": {
          "tokenizer": "standard",
          "filter": ["lowercase", "double_metaphone"]
        }
      }
    }
  }
}
```

**Note**: Requires `sudo bin/elasticsearch-plugin install analysis-phonetic` on the voice server.

---

## 4. N-gram Sub-field (Priority: Medium)

**Problem**: Partial matches fail — a caller saying "Atlan" (truncated STT) won't match "Atlantis".

**Solution**: Add an `ngram` sub-field for partial/prefix matching.

```json
{
  "settings": {
    "analysis": {
      "analyzer": {
        "ngram_analyzer": {
          "tokenizer": "standard",
          "filter": ["lowercase", "edge_ngram_filter"]
        }
      },
      "filter": {
        "edge_ngram_filter": {
          "type": "edge_ngram",
          "min_gram": 3,
          "max_gram": 10
        }
      }
    }
  }
}
```

```json
{
  "mappings": {
    "properties": {
      "name": {
        "type": "text",
        "analyzer": "turkish_custom",
        "fields": {
          "ngram": {
            "type": "text",
            "analyzer": "ngram_analyzer",
            "search_analyzer": "standard"
          }
        }
      }
    }
  }
}
```

---

## 5. Query-Time Boosts (Priority: High)

**Problem**: The hotel resolver needs to rank exact matches above fuzzy/phonetic/ngram matches.

**Solution**: Use a `bool` query with graduated boosts.

```json
{
  "query": {
    "bool": {
      "should": [
        { "match": { "name":          { "query": "rixos premium", "boost": 10 } } },
        { "match": { "aliases":       { "query": "rixos premium", "boost": 8 } } },
        { "match": { "name.phonetic": { "query": "rixos premium", "boost": 3 } } },
        { "match": { "name.ngram":    { "query": "rixos premium", "boost": 1 } } }
      ],
      "minimum_should_match": 1
    }
  }
}
```

**Tuning**: Adjust boost values after testing with real STT transcriptions. The `aliases` field holds known alternate names from `data/seed/hotels.json`.

---

## 6. Completion Suggester (Priority: Low)

**Problem**: For chat-mode or auto-complete scenarios, users typing partial hotel names need instant suggestions.

**Solution**: Add a `completion` field for the suggest API.

```json
{
  "mappings": {
    "properties": {
      "suggest": {
        "type": "completion",
        "analyzer": "simple",
        "contexts": [
          { "name": "region", "type": "category" }
        ]
      }
    }
  }
}
```

**Usage**: Populate with hotel name + aliases at index time. Query with `_suggest` API for sub-10ms autocomplete.

---

## 7. Hardware & Configuration (Priority: Low for dev, High for prod)

| Setting | Current | Recommended (Prod) |
|---------|---------|---------------------|
| Heap size | 512 MB | 1 GB (max 50% of RAM) |
| Refresh interval | 1s (default) | 30s (fewer segments, faster bulk loads) |
| Replicas | 0 | 1 (when adding a second node) |
| `index.max_result_window` | 10000 | 100 (we never paginate beyond top-10) |
| Disk type | Standard | SSD (for production latency) |

For the current 10-hotel dataset, hardware is not a bottleneck. These settings matter when scaling to 100+ hotels with thousands of chunks.

---

## Implementation Order

1. **keyword_marker** — immediate fix for brand name stemming (blocks Phase 1 resolver accuracy)
2. **synonym filter** — handles common caller variations
3. **query-time boosts** — resolver ranking logic
4. **phonetic analyzer** — catches STT mispronunciations
5. **ngram sub-field** — partial match fallback
6. **completion suggester** — only if chat-mode UI needs it
7. **hardware tuning** — before production launch

---

## Applying Changes

Since ES index settings/mappings are immutable after creation, applying these changes requires:

```bash
# 1. Close the index (or delete and recreate)
curl -X POST "http://$ES_HOST:9200/hotels/_close"

# 2. Update settings
curl -X PUT "http://$ES_HOST:9200/hotels/_settings" -H 'Content-Type: application/json' -d @new_settings.json

# 3. Reopen
curl -X POST "http://$ES_HOST:9200/hotels/_open"

# 4. Reindex all documents (required for analyzer changes to take effect on existing data)
# Use the call_center admin UI: Load → ES
```

For mapping changes (adding new sub-fields), delete and recreate the index, then reload via the admin UI.

---

## Validation

After applying optimisations, verify with these test cases:

| Input (STT transcript) | Expected hotel_id | Tests |
|-------------------------|-------------------|-------|
| "Rixos'tayım" | rixos_premium_belek | keyword_marker + stemmer |
| "Rıksos otel" | rixos_premium_belek | phonetic |
| "max royal" | maxx_royal_belek | synonym |
| "Atlan..." | limak_atlantis | ngram |
| "kornelya" | cornelia_de_luxe | phonetic + synonym |
| "Gloria spor" | gloria_sports_arena | synonym |
