"""System prompts and multilingual greetings for the Voxtera voice agent."""

from voxtera.prompts.greetings import (
    GREETINGS,
    TIMED_GREETINGS,
    daypart_for_hour,
    daypart_for_timezone,
    resolve_greeting,
)
from voxtera.prompts.system_prompt import SYSTEM_PROMPT

__all__ = [
    "GREETINGS",
    "SYSTEM_PROMPT",
    "TIMED_GREETINGS",
    "daypart_for_hour",
    "daypart_for_timezone",
    "resolve_greeting",
]
