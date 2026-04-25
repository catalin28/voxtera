"""Smoke tests for the Voxtera scaffold.

These tests deliberately avoid hitting any external API. They verify that the
package is importable and that core modules are wired up correctly. Real
integration tests for the voice loop will be added with VOX-6.

We use pytest's `monkeypatch` fixture rather than `unittest.mock.patch.dict`
because `uv run` auto-loads `.env` into the process environment, and
monkeypatch's per-test isolation handles that cleanly across local + CI.
"""

from __future__ import annotations

import pytest


def test_package_imports() -> None:
    import voxtera

    assert voxtera.__version__


def test_system_prompt_present() -> None:
    from voxtera.prompts import SYSTEM_PROMPT

    assert "Voxtera" in SYSTEM_PROMPT
    assert "language" in SYSTEM_PROMPT.lower()


def test_load_settings_requires_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from voxtera.config import load_settings

    # Explicitly remove the keys that load_settings() requires. monkeypatch
    # restores prior state at test teardown.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        load_settings()


def test_load_settings_with_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from voxtera.config import load_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("BOT_NAME", "TestBot")
    monkeypatch.setenv("DEFAULT_TTS_VOICE", "alloy")
    monkeypatch.setenv("VAD_STOP_SECS", "1.2")

    settings = load_settings()

    assert settings.anthropic_api_key == "test-anthropic"
    assert settings.openai_api_key == "test-openai"
    assert settings.log_level == "DEBUG"
    assert settings.bot_name == "TestBot"
    assert settings.default_tts_voice == "alloy"
    assert settings.vad_stop_secs == 1.2
