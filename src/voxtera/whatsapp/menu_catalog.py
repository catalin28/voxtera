"""Menu PDF catalog for WhatsApp — send a restaurant's menu as a document.

The voice/text concierge can't read a full menu aloud (40+ dishes), so instead
it gives a few highlights and offers to send the menu. When the guest accepts,
we deliver the restaurant's menu PDF to their WhatsApp chat.

Mirrors image_catalog, but for PDF *documents* with a file per language:

Catalog JSON (assets/menus/catalog.json)
----------------------------------------
{
  "menus": [
    {
      "id":          "tugra",
      "restaurant":  "Tuğra",
      "description": "Ottoman & Turkish fine dining …",
      "files":       {"en": "assets/menus/en/tugra.pdf", "tr": "assets/menus/tr/tugra.pdf"}
    }
  ]
}

Workflow
--------
1. ``system_prompt_block()`` injects the menu list + the [MENU:<id>] rule into
   the render prompt (voice only, gated by the caller channel).
2. The render gives highlights and appends a hidden ``[MENU:<id>]`` tag.
3. ``extract_menu_tag()`` strips it; the channel saves a pending menu offer.
4. On an affirmative reply, ``resolve_media_id(id, language)`` uploads the
   language-appropriate PDF once and the channel sends it via send_document.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

# Reuse the multilingual affirmative detector — same yes-signals as photos.
from voxtera.whatsapp.image_catalog import is_affirmative  # noqa: F401 (re-exported)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CATALOG_PATH = _REPO_ROOT / "assets" / "menus" / "catalog.json"

_catalog_cache: tuple[float, list[dict[str, Any]]] | None = None


def _load_catalog() -> list[dict[str, Any]]:
    """Return menu entries, re-reading the file when it changes on disk."""
    global _catalog_cache  # noqa: PLW0603
    if not _CATALOG_PATH.exists():
        return []
    mtime = _CATALOG_PATH.stat().st_mtime
    if _catalog_cache is not None and _catalog_cache[0] == mtime:
        return _catalog_cache[1]
    try:
        data = json.loads(_CATALOG_PATH.read_bytes().decode("utf-8"))
        entries: list[dict[str, Any]] = data.get("menus", [])
        valid = []
        for e in entries:
            files = e.get("files") or {}
            resolved = {}
            for lang, raw in files.items():
                p = Path(raw) if Path(raw).is_absolute() else _REPO_ROOT / raw
                if p.exists():
                    resolved[lang] = p
                else:
                    logger.warning("[menu-catalog] file missing, skipping: {}", p)
            if resolved:
                valid.append({**e, "_resolved_files": resolved})
        _catalog_cache = (mtime, valid)
        logger.info("[menu-catalog] loaded {} menu(s)", len(valid))
        return valid
    except Exception as exc:  # noqa: BLE001
        logger.error("[menu-catalog] failed to load catalog: {}", exc)
        return []


# ---------------------------------------------------------------------------
# Render system-prompt block
# ---------------------------------------------------------------------------
_MENU_TAG_RULE = """\
MENU REQUESTS — When a guest asks for a restaurant's menu, do NOT read the whole \
menu aloud. Give 2-3 enticing highlights in one or two sentences, then offer to \
send the full menu to their WhatsApp chat (e.g. "Shall I send the full menu to \
your chat?" — vary the phrasing, match the guest's language). Append the hidden \
tag [MENU:<id>] at the very end (after all visible text). One menu offer per \
reply. Never mention the tag. Available menus:\
"""


def system_prompt_block() -> str:
    """Menu-catalog block for the render system prompt (empty if no menus)."""
    entries = _load_catalog()
    if not entries:
        return ""
    lines = [_MENU_TAG_RULE]
    for e in entries:
        lines.append(f"  - [MENU:{e['id']}] — {e['restaurant']}: {e.get('description', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tag extraction  ([MENU:<id>])
# ---------------------------------------------------------------------------
_MENU_RE = re.compile(r"\[MENU:([^\]]+)\]")


def extract_menu_tag(text: str) -> tuple[str, str | None]:
    """Strip the first ``[MENU:<id>]`` tag; return (clean_text, id-or-None)."""
    match = _MENU_RE.search(text)
    if not match:
        return text.strip(), None
    menu_id = match.group(1)
    clean = _MENU_RE.sub("", text).strip()
    if menu_id not in {e["id"] for e in _load_catalog()}:
        logger.warning("[menu-catalog] LLM offered unknown menu id: {!r}", menu_id)
        return clean, None
    return clean, menu_id


# ---------------------------------------------------------------------------
# Pending offer store (per wa_id) — separate namespace from image offers
# ---------------------------------------------------------------------------
_pending_menus: dict[str, str] = {}


def set_pending_menu(wa_id: str, menu_id: str) -> None:
    _pending_menus[wa_id] = menu_id
    logger.debug("[menu-catalog] pending menu set: {} → {}", wa_id, menu_id)


def pop_pending_menu(wa_id: str) -> str | None:
    return _pending_menus.pop(wa_id, None)


def clear_pending_menu(wa_id: str) -> None:
    _pending_menus.pop(wa_id, None)


# ---------------------------------------------------------------------------
# Display helpers + media upload cache (per id+language)
# ---------------------------------------------------------------------------
def restaurant_name(menu_id: str) -> str | None:
    for e in _load_catalog():
        if e["id"] == menu_id:
            return e.get("restaurant") or menu_id
    return None


def filename_for(menu_id: str, language: str | None) -> str:
    """Guest-facing document filename, e.g. 'Tugra-Menu.pdf'."""
    name = (restaurant_name(menu_id) or menu_id).replace(" ", "-")
    # ASCII-fold for a clean filename (some clients mangle non-ASCII names).
    import unicodedata

    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii") or menu_id
    )
    return f"{ascii_name}-Menu.pdf"


_media_id_cache: dict[str, str] = {}  # "<id>:<lang>" → WhatsApp media_id


async def resolve_media_id(
    menu_id: str, *, language: str | None, settings: Any
) -> str | None:
    """Return the WhatsApp media_id for a menu PDF, uploading once per id+lang.

    Falls back to English when the requested language has no file.
    """
    entries = {e["id"]: e for e in _load_catalog()}
    entry = entries.get(menu_id)
    if entry is None:
        logger.error("[menu-catalog] resolve_media_id: unknown id {!r}", menu_id)
        return None
    files = entry.get("_resolved_files") or {}
    lang = (language or "en").lower()
    path = files.get(lang) or files.get("en") or next(iter(files.values()), None)
    if path is None:
        return None
    effective_lang = lang if lang in files else ("en" if "en" in files else next(iter(files)))
    cache_key = f"{menu_id}:{effective_lang}"
    if cache_key in _media_id_cache:
        return _media_id_cache[cache_key]

    from voxtera.whatsapp.client import WhatsAppClient

    try:
        async with aiohttp.ClientSession() as http:
            client = WhatsAppClient(settings=settings, session=http)
            media_id = await client.upload_media(path)
        _media_id_cache[cache_key] = media_id
        logger.info(
            "[menu-catalog] uploaded {} ({}) → media_id={}", menu_id, effective_lang, media_id
        )
        return media_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("[menu-catalog] upload failed for {}: {}", menu_id, exc)
        return None


def clear_media_id_cache() -> None:
    _media_id_cache.clear()
