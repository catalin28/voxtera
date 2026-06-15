"""Per-hotel prompt override: prompts/<hotel_id>/<name>.md beats the global one."""

from __future__ import annotations

import voxtera.call_center.prompts as prompts
from voxtera.call_center.prompts import load_prompt


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "_PROMPTS_DIR", tmp_path)
    prompts._cache.clear()
    (tmp_path / "concierge_render.md").write_text("GLOBAL render", encoding="utf-8")


def test_falls_back_to_global_when_no_override(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert load_prompt("concierge_render").strip() == "GLOBAL render"
    # hotel_id given but no override file → still the global text
    assert load_prompt("concierge_render", "kempinski_ciragan").strip() == "GLOBAL render"


def test_per_hotel_override_takes_precedence(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    hotel_dir = tmp_path / "kempinski_ciragan"
    hotel_dir.mkdir()
    (hotel_dir / "concierge_render.md").write_text("KEMPINSKI render", encoding="utf-8")

    assert load_prompt("concierge_render", "kempinski_ciragan").strip() == "KEMPINSKI render"
    # a different hotel without its own file → global
    assert load_prompt("concierge_render", "other_hotel").strip() == "GLOBAL render"
    # no hotel_id → global
    assert load_prompt("concierge_render").strip() == "GLOBAL render"


def test_override_only_affects_named_files(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    (tmp_path / "concierge_persona.md").write_text("GLOBAL persona", encoding="utf-8")
    hotel_dir = tmp_path / "kempinski_ciragan"
    hotel_dir.mkdir()
    # Override only the render, NOT the persona.
    (hotel_dir / "concierge_render.md").write_text("KEMPINSKI render", encoding="utf-8")

    assert load_prompt("concierge_render", "kempinski_ciragan").strip() == "KEMPINSKI render"
    # persona has no override → falls back to global
    assert load_prompt("concierge_persona", "kempinski_ciragan").strip() == "GLOBAL persona"


def test_with_persona_uses_override(tmp_path, monkeypatch):
    from voxtera.call_center.concierge import _with_persona

    _setup(tmp_path, monkeypatch)
    (tmp_path / "concierge_persona.md").write_text("GLOBAL persona", encoding="utf-8")
    hotel_dir = tmp_path / "kempinski_ciragan"
    hotel_dir.mkdir()
    (hotel_dir / "concierge_persona.md").write_text("KEMPINSKI persona", encoding="utf-8")

    text = _with_persona("concierge_render", include_images=False, hotel_id="kempinski_ciragan")
    assert "KEMPINSKI persona" in text
    assert "GLOBAL render" in text  # task prompt has no override → global
    # Without the hotel_id, the global persona is used.
    assert "GLOBAL persona" in _with_persona("concierge_render", include_images=False)
