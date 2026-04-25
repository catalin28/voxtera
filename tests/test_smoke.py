"""Smoke tests for the Voxtera scaffold.

These tests deliberately avoid hitting any external API. They verify that the
package is importable and that core modules are wired up correctly. Real
integration tests for the voice loop will be added with VOX-6.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest


def test_package_imports() -> None:
    import voxtera

    assert voxtera.__version__


def test_system_prompt_present() -> None:
    from voxtera.prompts import SYSTEM_PROMPT

    assert "Voxtera" in SYSTEM_PROMPT
    assert "language" in SYSTEM_PROMPT.lower()


def test_load_settings_requires_keys() -> None:
    from voxtera.config import load_settings

    with (
        mock.patch.dict(os.environ, {}, clear=True),
        pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"),
    ):
        load_settings()


def test_load_settings_with_keys() -> None:
    from voxtera.config import load_settings

    env = {
        "ANTHROPIC_API_KEY": "test-anthropic",
        "OPENAI_API_KEY": "test-openai",
        "LOG_LEVEL": "DEBUG",
        "BOT_NAME": "TestBot",
        "DEFAULT_TTS_VOICE": "alloy",
        "VAD_STOP_SECS": "1.2",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        settings = load_settings()

    assert settings.anthropic_api_key == "test-anthropic"
    assert settings.openai_api_key == "test-openai"
    assert settings.log_level == "DEBUG"
    assert settings.bot_name == "TestBot"
    assert settings.default_tts_voice == "alloy"
    assert settings.vad_stop_secs == 1.2
