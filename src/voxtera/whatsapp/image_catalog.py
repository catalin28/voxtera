"""Dynamic image catalog for WhatsApp visual responses.

Images are configured in ``assets/images/catalog.json`` — edit that file to
add, remove, or update images without touching Python source. The catalog
hot-reloads on file change (mtime check), so a running server picks up edits
on the next request without a restart.

Workflow
--------
1. The catalog is loaded and its entries injected into the LLM render system
   prompt so the model knows which images exist and when to surface them.
2. The LLM signals "show this image" by embedding ``[IMG:<id>]`` anywhere in
   its reply text (typically at the end, per prompt instructions).
3. ``extract_image_tags()`` strips those tags from the text and returns the
   matched image ids.
4. ``resolve_media_id()`` uploads the local file to the WhatsApp media store
   (once per image per process lifetime) and returns the cached ``media_id``
   for sending.

Catalog JSON schema
-------------------
{
  "images": [
    {
      "id":          "lobby",                           // unique key used in [IMG:lobby]
      "path":        "assets/images/hotel_hallway.jpg", // relative to repo root
      "description": "Grand Lumière main lobby — ..."   // shown to the LLM
    },
    ...
  ]
}

Paths are relative to the repo root (two parents above this file's package).
Absolute paths are also accepted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

# Repo root: src/voxtera/whatsapp/image_catalog.py → ../../../../
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CATALOG_PATH = _REPO_ROOT / "assets" / "images" / "catalog.json"

# ---------------------------------------------------------------------------
# Hot-reload cache
# ---------------------------------------------------------------------------
_catalog_cache: tuple[float, list[dict[str, Any]]] | None = None


def _load_catalog() -> list[dict[str, Any]]:
    """Return catalog entries, re-reading the file when it changes on disk."""
    global _catalog_cache  # noqa: PLW0603

    if not _CATALOG_PATH.exists():
        return []

    mtime = _CATALOG_PATH.stat().st_mtime
    if _catalog_cache is not None and _catalog_cache[0] == mtime:
        return _catalog_cache[1]

    try:
        data = json.loads(_CATALOG_PATH.read_bytes().decode("utf-8"))
        entries: list[dict[str, Any]] = data.get("images", [])
        # Resolve each path relative to repo root; filter missing files.
        valid = []
        for entry in entries:
            raw_path = entry.get("path", "")
            p = Path(raw_path) if Path(raw_path).is_absolute() else _REPO_ROOT / raw_path
            if not p.exists():
                logger.warning("[image-catalog] image not found, skipping: {}", p)
                continue
            valid.append({**entry, "_resolved_path": p})
        _catalog_cache = (mtime, valid)
        logger.info("[image-catalog] loaded {} image(s) from catalog", len(valid))
        return valid
    except Exception as e:  # noqa: BLE001
        logger.error("[image-catalog] failed to load catalog: {}", e)
        return []


# ---------------------------------------------------------------------------
# System prompt block
# ---------------------------------------------------------------------------
_IMG_TAG_RULE = (
    "IMAGES — You can show the guest a photo by placing [IMG:<id>] "
    "at the very END of your reply, after the last sentence. "
    "Use it only when a photo genuinely adds value (showing a space or view the guest asked about). "
    "Use at most one image per reply. Omit entirely if no image adds clear value. "
    "Never place [IMG:…] mid-sentence. Available images:"
)


def system_prompt_block() -> str:
    """Return the image catalog block to append to the LLM render system prompt.

    Returns an empty string when the catalog is empty (feature disabled or no
    images configured) so the prompt is unchanged in that case.
    """
    entries = _load_catalog()
    if not entries:
        return ""

    lines = [_IMG_TAG_RULE]
    for e in entries:
        lines.append(f"  - [IMG:{e['id']}] — {e['description']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tag extraction
# ---------------------------------------------------------------------------
_IMG_RE = re.compile(r"\[IMG:([^\]]+)\]")


def extract_image_tags(text: str) -> tuple[str, list[str]]:
    """Strip ``[IMG:<id>]`` tags from *text* and return (clean_text, [ids]).

    Unknown ids (not in the current catalog) are logged and dropped — we
    never send a stale reference that would error on the Graph API.
    """
    found = _IMG_RE.findall(text)
    clean = _IMG_RE.sub("", text).strip()

    catalog_ids = {e["id"] for e in _load_catalog()}
    valid_ids: list[str] = []
    for img_id in found:
        if img_id in catalog_ids:
            valid_ids.append(img_id)
        else:
            logger.warning("[image-catalog] LLM referenced unknown image id: {!r}", img_id)

    return clean, valid_ids


# ---------------------------------------------------------------------------
# Media ID cache (upload-once per image per process)
# ---------------------------------------------------------------------------
_media_id_cache: dict[str, str] = {}  # image_id → WhatsApp media_id


async def resolve_media_id(
    image_id: str,
    *,
    settings: Any,  # WhatsAppSettings — imported lazily to avoid circular import
) -> str | None:
    """Return the WhatsApp media_id for *image_id*, uploading if not cached.

    Returns None when the image_id is unknown or the upload fails.
    """
    if image_id in _media_id_cache:
        return _media_id_cache[image_id]

    entries = {e["id"]: e for e in _load_catalog()}
    entry = entries.get(image_id)
    if entry is None:
        logger.error("[image-catalog] resolve_media_id: unknown id {!r}", image_id)
        return None

    from voxtera.whatsapp.client import WhatsAppClient

    try:
        async with aiohttp.ClientSession() as http:
            client = WhatsAppClient(settings=settings, session=http)
            media_id = await client.upload_media(entry["_resolved_path"])
        _media_id_cache[image_id] = media_id
        logger.info("[image-catalog] uploaded {} → media_id={}", image_id, media_id)
        return media_id
    except Exception as e:  # noqa: BLE001
        logger.warning("[image-catalog] upload failed for {}: {}", image_id, e)
        return None


def clear_media_id_cache() -> None:
    """Force re-upload on next use (e.g. after catalog edits). Call from tests."""
    _media_id_cache.clear()
