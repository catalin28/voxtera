"""Website-concierge subproject: inbound sales voice agent support services.

Phase 1 ships the Leads API — the single write path in front of the
``leads_calls`` MySQL table that doubles as call log and lead store. See
``docs/website-concierge/architecture.md`` and ``PRD.md``.
"""

from __future__ import annotations

from voxtera.concierge.config import ConciergeSettings, load_concierge_settings
from voxtera.concierge.leads_api import create_app

__all__ = ["ConciergeSettings", "create_app", "load_concierge_settings"]
