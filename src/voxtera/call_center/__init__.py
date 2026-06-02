"""Hotel Call Center RAG admin server.

Standalone aiohttp server that provides:
- API endpoints to load/browse/search data in Elasticsearch and Qdrant
- A simple HTML UI for visual inspection

All routes are prefixed with /call_center/ to differentiate from
the main application endpoints.

Run standalone:
    uv run python -m voxtera.call_center.server
"""
