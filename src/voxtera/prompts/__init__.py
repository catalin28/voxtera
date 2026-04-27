"""System prompts and multilingual greetings for the Voxtera voice agent."""

from voxtera.prompts.greetings import GREETINGS, resolve_greeting
from voxtera.prompts.system_prompt import SYSTEM_PROMPT

__all__ = ["GREETINGS", "SYSTEM_PROMPT", "resolve_greeting"]
