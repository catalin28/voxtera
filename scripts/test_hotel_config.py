"""Manual smoke test: load a hotel config and print its contents.

Run from the project root:

    uv run python scripts/test_hotel_config.py [hotel_id]

If ``hotel_id`` is omitted, defaults to ``"demo"``.

This exercises three things at once:
    1. The YAML file in ``config/hotels/`` exists and parses cleanly.
    2. All required fields are present.
    3. Every entry in ``allowed_categories`` maps to a valid Category enum.
"""

from __future__ import annotations

import sys

from loguru import logger

from voxtera.actions import load_hotel_config


def main() -> int:
    hotel_id = sys.argv[1] if len(sys.argv) > 1 else "demo"
    try:
        config = load_hotel_config(hotel_id)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Failed to load hotel config: {}", e)
        return 1

    logger.info("✓ Hotel config loaded successfully:")
    logger.info("  hotel_id:            {}", config.hotel_id)
    logger.info("  hotel_name:          {}", config.hotel_name)
    logger.info("  official_language:   {}", config.official_language)
    logger.info("  telegram_channel_id: {}", config.telegram_channel_id)
    logger.info(
        "  allowed_categories:  [{}]",
        ", ".join(c.value for c in config.allowed_categories),
    )
    logger.info(
        "  system_prompt_addendum: {} chars",
        len(config.system_prompt_addendum) if config.system_prompt_addendum else 0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
