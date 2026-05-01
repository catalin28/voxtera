"""Per-category staff directory for the interactive actions feature.

Loaded once at startup from ``config/staff/<hotel_id>.yaml``. The directory
maps each :class:`~voxtera.actions.ticket.Category` to an ordered list of
:class:`Staff` available for that category. Order is preserved so the
Telegram inline-keyboard ordering is predictable for staff.

This module is data-only — no network, no Telegram, no LLM. The button
action handlers in :mod:`voxtera.actions.button_actions` consume it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from voxtera.actions.ticket import Category


@dataclass(frozen=True)
class Staff:
    """A single staff member available for a category."""

    name: str
    role: str
    eta_min: int
    # Free-form tags. Future v3: filter by skill matched against the
    # ticket summary (e.g. "no hot water" → require `plumbing`). For v2
    # we just display all staff for the category.
    skills: tuple[str, ...] = ()
    # Telegram chat_id for direct messages. ``None`` means we have no
    # binding for this staff yet — they'll still appear in the picker, but
    # the DM-on-assign step is skipped for them. Use
    # ``scripts/get_telegram_chat_id.py`` to discover it (staff DMs the
    # bot once, the script prints the chat_id to copy into YAML).
    telegram_chat_id: int | None = None

    def button_label(self) -> str:
        """Render the inline-keyboard button label for this staff member."""
        return f"👤 {self.name} — ETA {self.eta_min} min"


def _project_root() -> Path:
    """Resolve project root by walking up from this file's location."""
    return Path(__file__).resolve().parents[3]


class StaffDirectory:
    """Read-only mapping from Category to a tuple of Staff.

    Construct with :meth:`for_hotel` (loads YAML) or directly via the
    constructor for tests.
    """

    def __init__(self, by_category: dict[Category, tuple[Staff, ...]]) -> None:
        # Frozen-ish: we copy the input so callers cannot mutate the
        # backing structure after construction.
        self._by_category: dict[Category, tuple[Staff, ...]] = dict(by_category)

    def get(self, category: Category) -> tuple[Staff, ...]:
        """Return the staff tuple for ``category``, or empty if none configured."""
        return self._by_category.get(category, ())

    def has(self, category: Category) -> bool:
        """True iff there is at least one staff configured for ``category``."""
        return bool(self._by_category.get(category))

    def categories(self) -> tuple[Category, ...]:
        """All categories this directory has staff for."""
        return tuple(self._by_category.keys())

    @classmethod
    def empty(cls) -> StaffDirectory:
        """A directory with no staff. Handlers should degrade gracefully on this."""
        return cls({})

    @classmethod
    def from_yaml(cls, path: Path) -> StaffDirectory:
        """Load and validate a staff YAML file.

        Categories that don't match a known :class:`Category` are skipped with
        a warning rather than raising — the YAML can be ahead of code (e.g.
        if a new category was added in the file but not yet in the enum).
        """
        if not path.is_file():
            raise FileNotFoundError(f"Staff config not found: {path}")
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(
                f"Staff config at {path} must be a YAML mapping, " f"got {type(data).__name__}"
            )

        by_category: dict[Category, tuple[Staff, ...]] = {}
        for raw_cat, raw_list in data.items():
            try:
                category = Category(raw_cat)
            except ValueError:
                logger.warning("[staff] unknown category {!r} in {} — skipping", raw_cat, path)
                continue
            if not isinstance(raw_list, list):
                raise ValueError(f"Staff config at {path}: value for {raw_cat!r} must be a list")
            staff_list = tuple(_parse_staff_entry(entry, raw_cat, path) for entry in raw_list)
            by_category[category] = staff_list

        total = sum(len(v) for v in by_category.values())
        logger.info(
            "[staff] loaded {} staff across {} categories from {}",
            total,
            len(by_category),
            path,
        )
        return cls(by_category)

    @classmethod
    def for_hotel(cls, hotel_id: str, config_dir: Path | None = None) -> StaffDirectory:
        """Convenience loader: ``config/staff/<hotel_id>.yaml`` from project root.

        Returns an :meth:`empty` directory if the file does not exist, so
        action handlers degrade gracefully — they show a "no staff
        configured" toast instead of raising.
        """
        if config_dir is None:
            config_dir = _project_root() / "config" / "staff"
        path = config_dir / f"{hotel_id}.yaml"
        if not path.is_file():
            logger.warning(
                "[staff] no staff file at {} — interactive 'Find available' will be empty",
                path,
            )
            return cls.empty()
        return cls.from_yaml(path)


def _parse_staff_entry(entry: object, category: str, source: Path) -> Staff:
    """Coerce one YAML staff entry into a Staff dataclass. Raises on bad input."""
    if not isinstance(entry, dict):
        raise ValueError(
            f"Staff config at {source}: each entry under {category!r} "
            f"must be a mapping, got {type(entry).__name__}"
        )
    try:
        name = str(entry["name"]).strip()
        role = str(entry.get("role", "")).strip() or "staff"
        eta_min = int(entry.get("eta_min", 5))
    except KeyError as e:
        raise ValueError(
            f"Staff config at {source}: missing required field {e} under {category!r}"
        ) from e
    if not name:
        raise ValueError(f"Staff config at {source}: empty name in entry under {category!r}")
    raw_skills = entry.get("skills") or ()
    if not isinstance(raw_skills, list | tuple):
        raise ValueError(
            f"Staff config at {source}: skills for {name!r} must be a list, "
            f"got {type(raw_skills).__name__}"
        )
    raw_chat_id = entry.get("telegram_chat_id")
    chat_id: int | None = None
    if raw_chat_id is not None:
        try:
            chat_id = int(raw_chat_id)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Staff config at {source}: telegram_chat_id for {name!r} "
                f"must be an integer, got {raw_chat_id!r}"
            ) from e
    return Staff(
        name=name,
        role=role,
        eta_min=eta_min,
        skills=tuple(str(s) for s in raw_skills),
        telegram_chat_id=chat_id,
    )
