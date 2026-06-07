"""System prompts and multilingual greetings for the Voxtera voice agent."""

from voxtera.prompts.greetings import (
    GREETINGS,
    TIMED_GREETINGS,
    apply_hotel_name,
    daypart_for_hour,
    daypart_for_timezone,
    fill_hotel_name,
    resolve_greeting,
)
from voxtera.prompts.system_prompt import SYSTEM_PROMPT

__all__ = [
    "GREETINGS",
    "SYSTEM_PROMPT",
    "TIMED_GREETINGS",
    "apply_hotel_name",
    "daypart_for_hour",
    "daypart_for_timezone",
    "fill_hotel_name",
    "resolve_greeting",
]
