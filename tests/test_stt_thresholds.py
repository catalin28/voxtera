"""Tests for the per-language Whisper confidence threshold loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voxtera.stt_thresholds import STTThresholds, _to_canonical

# --- canonical resolution ----------------------------------------------------


def test_canonical_resolves_full_english_names() -> None:
    assert _to_canonical("english") == "en"
    assert _to_canonical("French") == "fr"
    assert _to_canonical("ROMANIAN") == "ro"
    assert _to_canonical("turkish") == "tr"
    assert _to_canonical("azerbaijani") == "az"
    assert _to_canonical("arabic") == "ar"


def test_canonical_resolves_short_codes() -> None:
    assert _to_canonical("en") == "en"
    assert _to_canonical("EN") == "en"
    assert _to_canonical("ru") == "ru"
    assert _to_canonical("ar") == "ar"


def test_canonical_resolves_bcp47_strips_subtag() -> None:
    assert _to_canonical("en-US") == "en"
    assert _to_canonical("en_GB") == "en"
    assert _to_canonical("ro-RO") == "ro"
    assert _to_canonical("zh-CN") == "zh"


def test_canonical_returns_none_for_unknown_or_empty() -> None:
    assert _to_canonical(None) is None
    assert _to_canonical("") is None
    assert _to_canonical("   ") is None
    assert _to_canonical("klingon") is None
    assert _to_canonical("xx-YY") is None


# --- file loading ------------------------------------------------------------


def test_load_none_path_uses_hardcoded_fallback() -> None:
    t = STTThresholds.load(None)
    th = t.for_language("en")
    fallback = STTThresholds.hardcoded_fallback()
    assert th == fallback


def test_load_missing_file_uses_hardcoded_fallback(tmp_path: Path) -> None:
    t = STTThresholds.load(tmp_path / "does-not-exist.json")
    fallback = STTThresholds.hardcoded_fallback()
    assert t.for_language("en") == fallback
    assert t.for_language("ru") == fallback
    assert t.for_language(None) == fallback


def test_load_malformed_json_uses_hardcoded_fallback(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("this is not valid json{{{", encoding="utf-8")
    t = STTThresholds.load(p)
    fallback = STTThresholds.hardcoded_fallback()
    assert t.for_language("en") == fallback


def test_load_non_object_top_level_uses_hardcoded(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    t = STTThresholds.load(p)
    fallback = STTThresholds.hardcoded_fallback()
    assert t.for_language("en") == fallback


# --- per-language lookup -----------------------------------------------------


def _write(p: Path, payload: dict) -> Path:
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_per_language_lookup_uses_explicit_entry(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "thresholds.json",
        {
            "default": {"avg_logprob_min": -1.0, "no_speech_prob_max": 0.7},
            "en": {"avg_logprob_min": -0.7, "no_speech_prob_max": 0.6},
            "tr": {"avg_logprob_min": -1.0, "no_speech_prob_max": 0.7},
        },
    )
    t = STTThresholds.load(p)
    assert t.for_language("en").avg_logprob_min == -0.7
    assert t.for_language("english").avg_logprob_min == -0.7
    assert t.for_language("EN").avg_logprob_min == -0.7
    assert t.for_language("tr").avg_logprob_min == -1.0


def test_unknown_language_falls_back_to_default(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "thresholds.json",
        {
            "default": {"avg_logprob_min": -0.95, "no_speech_prob_max": 0.65},
            "en": {"avg_logprob_min": -0.7, "no_speech_prob_max": 0.6},
        },
    )
    t = STTThresholds.load(p)
    de = t.for_language("german")
    assert de.avg_logprob_min == -0.95
    assert de.no_speech_prob_max == 0.65


def test_no_default_entry_uses_hardcoded_for_unknown(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "thresholds.json",
        {"en": {"avg_logprob_min": -0.7, "no_speech_prob_max": 0.6}},
    )
    t = STTThresholds.load(p)
    en = t.for_language("en")
    assert en.avg_logprob_min == -0.7  # explicit entry still works
    de = t.for_language("german")
    fallback = STTThresholds.hardcoded_fallback()
    assert de == fallback


def test_first_turn_no_language_uses_default(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "thresholds.json",
        {
            "default": {"avg_logprob_min": -0.9, "no_speech_prob_max": 0.65},
            "en": {"avg_logprob_min": -0.7, "no_speech_prob_max": 0.6},
        },
    )
    t = STTThresholds.load(p)
    th = t.for_language(None)
    assert th.avg_logprob_min == -0.9
    assert th.no_speech_prob_max == 0.65


def test_unknown_language_key_in_json_is_ignored(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "thresholds.json",
        {
            "default": {"avg_logprob_min": -1.0, "no_speech_prob_max": 0.7},
            "klingon": {"avg_logprob_min": -0.5, "no_speech_prob_max": 0.5},
            "en": {"avg_logprob_min": -0.7, "no_speech_prob_max": 0.6},
        },
    )
    t = STTThresholds.load(p)
    # English entry preserved
    assert t.for_language("en").avg_logprob_min == -0.7
    # Klingon was silently dropped; nothing else should have absorbed its values
    assert "klingon" not in t.configured_languages()


def test_invalid_entry_is_ignored_but_others_load(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "thresholds.json",
        {
            "default": {"avg_logprob_min": -1.0, "no_speech_prob_max": 0.7},
            "en": {"avg_logprob_min": "not-a-number", "no_speech_prob_max": 0.6},
            "fr": {"avg_logprob_min": -0.7, "no_speech_prob_max": 0.6},
        },
    )
    t = STTThresholds.load(p)
    # Bad entry → falls back to default
    assert t.for_language("en") == t.default
    # Good entry still works
    assert t.for_language("fr").avg_logprob_min == -0.7


def test_full_english_name_keys_in_json(tmp_path: Path) -> None:
    """JSON keys can be either short codes OR full English names."""
    p = _write(
        tmp_path / "thresholds.json",
        {
            "default": {"avg_logprob_min": -1.0, "no_speech_prob_max": 0.7},
            "english": {"avg_logprob_min": -0.65, "no_speech_prob_max": 0.55},
        },
    )
    t = STTThresholds.load(p)
    assert t.for_language("en").avg_logprob_min == -0.65
    assert t.for_language("english").avg_logprob_min == -0.65


# --- reload ------------------------------------------------------------------


def test_reload_picks_up_file_changes(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "thresholds.json",
        {
            "default": {"avg_logprob_min": -1.0, "no_speech_prob_max": 0.7},
            "en": {"avg_logprob_min": -0.7, "no_speech_prob_max": 0.6},
        },
    )
    t = STTThresholds.load(p)
    assert t.for_language("en").avg_logprob_min == -0.7

    _write(
        p,
        {
            "default": {"avg_logprob_min": -1.0, "no_speech_prob_max": 0.7},
            "en": {"avg_logprob_min": -0.5, "no_speech_prob_max": 0.5},
        },
    )
    t.reload()
    assert t.for_language("en").avg_logprob_min == -0.5
    assert t.for_language("en").no_speech_prob_max == 0.5


def test_reload_with_no_path_is_safe_noop() -> None:
    t = STTThresholds.load(None)
    # Should not raise.
    t.reload()
    assert t.for_language("en") == STTThresholds.hardcoded_fallback()


# --- ships-with-repo config sanity check -------------------------------------


def test_shipped_config_loads_for_demo_languages() -> None:
    """Smoke-check the config file we ship with the repo.

    Validates that config/stt_thresholds.json (the file the bot loads by
    default) parses and contains entries for each of the languages we
    intend to demo.
    """
    repo_root = Path(__file__).resolve().parent.parent
    p = repo_root / "config" / "stt_thresholds.json"
    if not p.exists():
        pytest.skip(f"shipped config not present at {p}")
    t = STTThresholds.load(p)
    expected = {"en", "fr", "ro", "ru", "tr", "az", "ar"}
    configured = set(t.configured_languages())
    missing = expected - configured
    assert not missing, f"shipped config missing demo languages: {missing}"
    # And the default entry should be present and lenient.
    assert t.default.avg_logprob_min <= -0.7
    assert t.default.no_speech_prob_max >= 0.5
