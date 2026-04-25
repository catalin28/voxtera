"""Main entry point for the Voxtera local voice loop.

This module is a placeholder for VOX-6 (Implement local voice loop with Whisper
+ Claude + OpenAI TTS). The architect will deliver the full Pipecat pipeline
implementation as the next deliverable.

For now, importing this module loads settings and logs that the bot is ready to
be implemented — useful as a smoke test for the project scaffold.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from loguru import logger

from voxtera.config import load_settings


def main() -> int:
    """Placeholder entry point. Replace with the Pipecat pipeline in VOX-6."""
    # Honour a local `.env` if present. `load_settings()` itself is pure and
    # only reads `os.environ`, which keeps it filesystem-isolated for tests.
    load_dotenv()

    try:
        settings = load_settings()
    except RuntimeError as exc:
        logger.error("Startup failed: {}", exc)
        return 1

    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)

    logger.info("Voxtera scaffold loaded.")
    logger.info("Bot name: {}", settings.bot_name)
    logger.info("Default TTS voice: {}", settings.default_tts_voice)
    logger.info("VAD stop_secs: {}", settings.vad_stop_secs)
    logger.info(
        "VOX-6 not yet implemented. The Pipecat pipeline (LocalAudioTransport → "
        "Silero VAD → Whisper STT → Claude LLM → OpenAI TTS) will be added here."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
