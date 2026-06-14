"""Menu PDF catalog — tag extraction, pending store, language file selection."""

from __future__ import annotations

from voxtera.whatsapp import menu_catalog as mc


def test_catalog_loads_expected_restaurants() -> None:
    ids = {e["id"] for e in mc._load_catalog()}
    assert {"tugra", "ruya", "gazebo", "bellini"}.issubset(ids)


def test_system_prompt_block_lists_menu_tags() -> None:
    block = mc.system_prompt_block()
    assert "[MENU:tugra]" in block
    assert "Tuğra" in block
    assert "send the full menu" in block.lower()


def test_extract_menu_tag_strips_and_returns_id() -> None:
    clean, mid = mc.extract_menu_tag("A few highlights for you. [MENU:tugra]")
    assert clean == "A few highlights for you."
    assert mid == "tugra"


def test_extract_menu_tag_no_tag() -> None:
    assert mc.extract_menu_tag("just text") == ("just text", None)


def test_extract_menu_tag_unknown_id_discarded() -> None:
    clean, mid = mc.extract_menu_tag("text [MENU:does_not_exist]")
    assert clean == "text"
    assert mid is None


def test_pending_menu_store_roundtrip() -> None:
    mc.set_pending_menu("wa1", "ruya")
    assert mc.pop_pending_menu("wa1") == "ruya"
    assert mc.pop_pending_menu("wa1") is None  # cleared after pop


def test_filename_is_ascii_folded() -> None:
    # Turkish display name must not leak non-ASCII into the WhatsApp filename.
    fn = mc.filename_for("tugra", "en")
    assert fn == "Tugra-Menu.pdf"
    assert fn.isascii()
    assert mc.filename_for("ruya", "tr") == "Ruya-Istanbul-Menu.pdf"


def test_restaurant_name_lookup() -> None:
    assert mc.restaurant_name("tugra") == "Tuğra"
    assert mc.restaurant_name("nope") is None
