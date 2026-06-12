"""Dynamic image catalog for WhatsApp visual responses.

Images are configured in ``assets/images/catalog.json`` — edit that file to
add, remove, or update images without touching Python source. The catalog
hot-reloads on file change (mtime check), so a running server picks up edits
on the next request without a restart.

Workflow
--------
1. The catalog is loaded and its entries injected into the LLM render system
   prompt so the model knows which images exist and when to surface them.
2. When the concierge describes a facility it has a photo of, it naturally asks
   the guest "Would you like to see a photo?" and embeds a hidden ``[OFFER:<id>]``
   tag at the end of the reply.
3. ``extract_offer_tag()`` strips the tag and returns the image id. The webhook
   saves it as a pending offer for that wa_id.
4. On the guest's next message: ``is_affirmative()`` checks for a yes-like reply
   (multilingual). If affirmative and a pending offer exists, the image is sent
   immediately — no concierge call needed.
5. ``resolve_media_id()`` uploads the local file to the WhatsApp media store
   once per image per process lifetime and returns the cached ``media_id``.

Catalog JSON schema
-------------------
{
  "images": [
    {
      "id":          "restaurant",
      "path":        "assets/images/restaurant.jpg",
      "description": "Le Lumière restaurant — candlelit tables, floor-to-ceiling windows"
    },
    ...
  ]
}

Paths are relative to the repo root. Absolute paths are also accepted.
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
# Hot-reload catalog loader
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
# System prompt block (injected into LLM render prompt)
# ---------------------------------------------------------------------------
_OFFER_TAG_RULE = """\
PHOTO OFFERS — When you describe a hotel space or facility that has a photo in \
the list below, end your reply by naturally offering to show it \
(e.g. "Would you like to see a photo?" or "Shall I show you a picture?" — \
vary the phrasing, keep it brief, match the guest's language). \
Then append the hidden tag [OFFER:<id>] at the very end (after all visible text). \
Use at most one offer per reply. Never mention the tag to the guest. \
Only offer when you have genuinely described the space — not for every reply. \
Available photos:\
"""


def system_prompt_block() -> str:
    """Return the image catalog block to append to the LLM render system prompt.

    Returns an empty string when the catalog is empty so the prompt is unchanged.
    """
    entries = _load_catalog()
    if not entries:
        return ""

    lines = [_OFFER_TAG_RULE]
    for e in entries:
        lines.append(f"  - [OFFER:{e['id']}] — {e['description']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Offer tag extraction  ([OFFER:<id>] — hidden LLM signal)
# ---------------------------------------------------------------------------
_OFFER_RE = re.compile(r"\[OFFER:([^\]]+)\]")


def extract_offer_tag(text: str) -> tuple[str, str | None]:
    """Strip the first ``[OFFER:<id>]`` tag from *text* and return (clean_text, id).

    Returns (text, None) when no valid offer tag is present.
    Unknown ids are logged and discarded so stale catalog entries never error.
    """
    match = _OFFER_RE.search(text)
    if not match:
        return text.strip(), None

    img_id = match.group(1)
    clean = _OFFER_RE.sub("", text).strip()

    catalog_ids = {e["id"] for e in _load_catalog()}
    if img_id not in catalog_ids:
        logger.warning("[image-catalog] LLM offered unknown image id: {!r}", img_id)
        return clean, None

    return clean, img_id


# ---------------------------------------------------------------------------
# Pending offer store  (in-memory, per wa_id)
# ---------------------------------------------------------------------------
# Maps wa_id → image_id the concierge just offered to show.
# Cleared when the guest accepts (image sent) or asks something unrelated.
_pending_offers: dict[str, str] = {}


def set_pending_offer(wa_id: str, image_id: str) -> None:
    _pending_offers[wa_id] = image_id
    logger.debug("[image-catalog] pending offer set: {} → {}", wa_id, image_id)


def pop_pending_offer(wa_id: str) -> str | None:
    """Return and clear the pending offer for *wa_id*, or None if none."""
    return _pending_offers.pop(wa_id, None)


def clear_pending_offer(wa_id: str) -> None:
    _pending_offers.pop(wa_id, None)


# ---------------------------------------------------------------------------
# Affirmative reply detection  (multilingual)
# ---------------------------------------------------------------------------
# Short list of clear yes-signals across the languages Voxtera supports.
# We only intercept short, clearly affirmative messages — anything complex
# falls through to the normal concierge so the LLM handles nuance.
_AFFIRMATIVES = {
    # English
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please",
    "absolutely", "of course", "definitely", "go ahead", "show me",
    "yes please", "yes, please", "send it", "show it",
    # French
    "oui", "bien sûr", "bien sur", "s'il vous plaît", "s'il vous plait",
    "avec plaisir", "volontiers",
    # Spanish
    "sí", "si", "claro", "por favor", "dale", "desde luego",
    # Portuguese
    "sim", "claro", "por favor", "com certeza",
    # Romanian
    "da", "sigur", "desigur", "bineînțeles", "bineinteles", "te rog",
    # German
    "ja", "bitte", "natürlich", "naturlich", "klar", "gerne",
    # Italian
    "sì", "si", "certo", "certamente", "per favore",
    # Turkish
    "evet", "tabii", "lütfen", "lutfen", "elbette",
    # Dutch
    "ja", "natuurlijk", "graag",
    # Arabic (transliterated)
    "نعم", "أكيد", "من فضلك",
    # Russian
    "да", "конечно", "пожалуйста",
    # Japanese
    "はい", "もちろん",
    # Chinese
    "是", "好的", "当然",
}


def is_affirmative(text: str) -> bool:
    """Return True if *text* looks like a clear yes to a photo offer.

    Matches case-insensitively against a multilingual affirmative list.
    Only short messages (≤ 6 words) are tested — longer messages are
    likely a new question and should go through the concierge instead.
    """
    stripped = text.strip().rstrip("!.,?")
    if len(stripped.split()) > 6:
        return False
    return stripped.lower() in _AFFIRMATIVES


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

    # Tour-only entries have no image to upload.
    if "_resolved_path" not in entry:
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


def get_tour_url(image_id: str) -> str | None:
    """Return the ``tour_url`` for *image_id* if the entry has one, else None."""
    entries = {e["id"]: e for e in _load_catalog()}
    entry = entries.get(image_id)
    return entry.get("tour_url") if entry else None


def clear_media_id_cache() -> None:
    """Force re-upload on next use (e.g. after catalog edits). Call from tests."""
    _media_id_cache.clear()
