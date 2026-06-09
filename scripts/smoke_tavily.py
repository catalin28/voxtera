"""One-off Tavily key check. Reads TAVILY_API_KEY from .env, runs one query."""
import json
import os
import sys
import urllib.request
from pathlib import Path


def load_env_key(name: str) -> str:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} not found in .env")


def main() -> int:
    key = load_env_key("TAVILY_API_KEY")
    query = " ".join(sys.argv[1:]) or "Rixos Premium Belek hotel reviews"
    body = {
        "api_key": key,
        "query": query,
        "max_results": 3,
        "include_answer": True,
    }
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
        print(f"STATUS: {r.status}")

    answer = (data.get("answer") or "").strip()
    print(f"\nANSWER:\n  {answer[:500] or '(no synthesized answer)'}\n")
    for i, h in enumerate(data.get("results", []), 1):
        print(f"[{i}] {h.get('title','')[:90]}")
        print(f"    {h.get('url','')}")
        snippet = (h.get("content") or "").replace("\n", " ")[:200]
        print(f"    {snippet}...\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
