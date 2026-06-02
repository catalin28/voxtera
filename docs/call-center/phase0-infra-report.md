# Phase 0 — Infrastructure Installation Report

**Date:** 2026-06-01
**Server:** `voice` (SSH alias)
**Operator:** Automated via Copilot

---

## Server Specs

| Field | Value |
|---|---|
| OS | Ubuntu 24.04.3 LTS (Noble Numbat) |
| RAM | 2 GB (1.4 GB used post-install) |
| Disk | 48 GB total, 21 GB free |
| User | root |

---

## Elasticsearch

| Field | Value |
|---|---|
| Version | 8.19.16 |
| Cluster name | `voxtera-rag` |
| Node name | `voice-node-1` |
| Status | **green** ✓ |
| HTTP port | 9200 |
| Transport port | 9300 |
| Heap | 512 MB (Xms/Xmx) |
| Discovery | single-node |
| Security | disabled (local dev) |
| Service | `systemctl` — enabled, active |

### Configuration

- **Config file:** `/etc/elasticsearch/elasticsearch.yml`
- **Heap options:** `/etc/elasticsearch/jvm.options.d/heap.options`
- **Data path:** `/var/lib/elasticsearch`
- **Log path:** `/var/log/elasticsearch`

### Turkish Analyzer Verification

```
Input:  "Rixosta rezervasyon Kaya'da Hilton'a"
Output: ["rixos", "rezervasyo", "ka", "hilto"]
```

Key result: `Rixosta` → `rixos` — Turkish suffix `-ta` correctly stripped.
Note: Over-stemming on proper names (`Kaya` → `ka`, `Hilton` → `hilto`) will be addressed in Phase 1 with a custom analyzer using keyword filters for brand names.

---

## Qdrant

| Field | Value |
|---|---|
| Version | 1.18.1 |
| HTTP port | 6333 |
| gRPC port | 6334 |
| Storage path | `/var/lib/qdrant/storage` |
| Snapshots path | `/var/lib/qdrant/snapshots` |
| Config | `/etc/qdrant/config.yaml` |
| Service | `systemctl` — enabled, active |

### Collections Created

| Collection | Vector size | Distance metric |
|---|---|---|
| `hotel_kb` | 1024 | Cosine |
| `destination_kb` | 1024 | Cosine |

Vector dimension 1024 matches `multilingual-e5-large` output.

---

## Remaining Phase 0 Deliverables

| Deliverable | Status |
|---|---|
| Elasticsearch with Turkish analyzer | ✅ Done |
| Qdrant with collections | ✅ Done |
| Redis with session key structure | ⬜ Not started |
| multilingual-e5-large loaded | ⬜ Not started |
| Docker Compose for local dev | ⬜ N/A (bare-metal on voice server) |
| FastAPI `/chat` skeleton | ⬜ Not started |
| `.env` structure documented | ⬜ Not started |
| 10-hotel seed dataset loaded | ⬜ Not started |

---

## Access

```bash
# Elasticsearch
ssh voice 'curl http://localhost:9200/'
ssh voice 'curl http://localhost:9200/_cluster/health'

# Qdrant
ssh voice 'curl http://localhost:6333/'
ssh voice 'curl http://localhost:6333/collections'

# Services
ssh voice 'systemctl status elasticsearch'
ssh voice 'systemctl status qdrant'
```

---

## Notes

- Security is disabled on ES for Phase 0 local dev. Must be enabled before any production deployment.
- Server has only 500 MB RAM available after both services. Redis install will require careful memory tuning (recommend `maxmemory 128mb`).
- Qdrant runs as root — consider creating a dedicated user for production.
- Both services are set to auto-restart on failure (`Restart=always`).

---

*Generated 2026-06-01 · Voxtera Phase 0*
