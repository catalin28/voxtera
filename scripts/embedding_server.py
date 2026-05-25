"""Embedding sidecar server — serves local embedding requests over HTTP.

This long-lived process loads the ONNX embedding model once at startup and
serves embedding requests from bot subprocesses via a simple JSON API on
localhost. This eliminates the 3-8s cold-start penalty that each bot
subprocess would otherwise pay when loading the model independently.

Usage:
    python scripts/embedding_server.py [--port 9400]

API:
    POST /embed
    Body: {"texts": ["query: what time is breakfast"], "prefix": "query: "}
    Response: {"embeddings": [[0.012, -0.034, ...], ...]}

    GET /health
    Response: {"ok": true, "model": "intfloat/multilingual-e5-small"}

Lifecycle: started by serve.py at boot; bot subprocesses set
VOXTERA_EMBEDDING_URL=http://127.0.0.1:9400 to use the sidecar instead of
loading the model locally.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Ensure project src is importable when run as a script.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from voxtera.rag.embeddings import (  # noqa: E402
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    embed_sync,
)

DEFAULT_PORT = 9400


class EmbeddingHandler(BaseHTTPRequestHandler):
    """Handles /embed and /health requests."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"ok": True, "model": EMBEDDING_MODEL, "dim": EMBEDDING_DIM})
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/embed":
            self._send_json(404, {"error": "not_found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json(400, {"error": "empty_body"})
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return

        texts = body.get("texts")
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            self._send_json(400, {"error": "texts must be a list of strings"})
            return

        prefix = body.get("prefix", "query: ")
        if not isinstance(prefix, str):
            self._send_json(400, {"error": "prefix must be a string"})
            return

        t0 = time.perf_counter()
        embeddings = embed_sync(texts, prefix=prefix)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        self._send_json(200, {"embeddings": embeddings, "elapsed_ms": round(elapsed_ms, 1)})

    def _send_json(self, status: int, data: dict) -> None:
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Suppress per-request logs; print only errors.
        if args and str(args[1]).startswith("4"):
            sys.stderr.write(f"[embedding-server] {args[0]} {args[1]}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Voxtera embedding sidecar server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on")
    args = parser.parse_args()

    # Eagerly load the model so bot requests are instant.
    print(f"[embedding-server] Loading model {EMBEDDING_MODEL}...")
    t0 = time.perf_counter()
    embed_sync(["warmup"])
    elapsed = time.perf_counter() - t0
    print(f"[embedding-server] Model loaded in {elapsed:.1f}s")

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", args.port), EmbeddingHandler)
    print(f"[embedding-server] Listening on http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[embedding-server] Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
