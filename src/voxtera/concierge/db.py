"""Async MySQL data layer for the Leads API.

Defines :class:`LeadsStore` (a Protocol the HTTP layer depends on) and
:class:`MySQLLeadsStore` (the aiomysql-backed implementation). Keeping the
HTTP layer dependent on the Protocol — not the concrete class — means tests
can inject an in-memory fake without a running MySQL or the ``aiomysql``
dependency installed.

``aiomysql`` is imported here and nowhere else in the package, so importing
``voxtera.concierge.leads_api`` does not require the driver to be present.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import aiomysql
from loguru import logger

from voxtera.concierge.config import ConciergeSettings

# Columns the API is allowed to write. Whitelisted so a caller can never inject
# arbitrary column names into the UPDATE/INSERT SET clause.
_WRITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "name",
        "email",
        "phone",
        "timezone",
        "booking_time",
        "calcom_booking_id",
        "notes",
    }
)


@runtime_checkable
class LeadsStore(Protocol):
    """Storage interface the Leads API depends on.

    Implemented by :class:`MySQLLeadsStore` in production and by a small fake
    in the tests. All methods are async.
    """

    async def record_call(self, *, caller_number: str | None, dialed_number: str | None) -> int:
        """Insert a new call row (status ``ringing``) and return its id."""
        ...

    async def update_lead(self, lead_id: int, **fields: Any) -> bool:
        """Update writable fields on an existing row. Returns True if a row matched."""
        ...

    async def create_lead(self, **fields: Any) -> int:
        """Insert a standalone lead (e.g. from a web form) and return its id."""
        ...

    async def list_leads(
        self, *, limit: int = 100, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the most recent rows, optionally filtered by status."""
        ...


def _filter_writable(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown/None keys, keeping only whitelisted, set values."""
    return {
        key: value for key, value in fields.items() if key in _WRITABLE_FIELDS and value is not None
    }


class MySQLLeadsStore:
    """aiomysql-backed :class:`LeadsStore`.

    Holds a connection pool. Call :meth:`connect` once at startup and
    :meth:`close` at shutdown (the aiohttp app wires these to its lifecycle).
    """

    def __init__(self, settings: ConciergeSettings) -> None:
        self._settings = settings
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        """Open the connection pool. Idempotent."""
        if self._pool is not None:
            return
        s = self._settings
        self._pool = await aiomysql.create_pool(
            host=s.db_host,
            port=s.db_port,
            user=s.db_user,
            password=s.db_password,
            db=s.db_name,
            minsize=s.db_pool_min,
            maxsize=s.db_pool_max,
            autocommit=True,
            charset="utf8mb4",
        )
        logger.info("Leads DB pool connected to {}:{}/{}", s.db_host, s.db_port, s.db_name)

    async def close(self) -> None:
        """Close the pool and wait for connections to drain."""
        if self._pool is None:
            return
        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None
        logger.info("Leads DB pool closed")

    @property
    def _require_pool(self) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError("MySQLLeadsStore.connect() was not called")
        return self._pool

    async def record_call(self, *, caller_number: str | None, dialed_number: str | None) -> int:
        async with self._require_pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO leads_calls (caller_number, dialed_number, status) "
                "VALUES (%s, %s, 'ringing')",
                (caller_number, dialed_number),
            )
            lead_id = cur.lastrowid
        logger.info("Logged inbound call id={} from={}", lead_id, caller_number)
        return int(lead_id)

    async def update_lead(self, lead_id: int, **fields: Any) -> bool:
        writable = _filter_writable(fields)
        if not writable:
            return False
        set_clause = ", ".join(f"{col} = %s" for col in writable)
        params = [*writable.values(), lead_id]
        async with self._require_pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                f"UPDATE leads_calls SET {set_clause} WHERE id = %s",  # noqa: S608 — cols are whitelisted
                params,
            )
            matched = cur.rowcount > 0
        logger.info("Updated lead id={} fields={} matched={}", lead_id, list(writable), matched)
        return matched

    async def create_lead(self, **fields: Any) -> int:
        writable = _filter_writable(fields)
        cols = ["status", *writable.keys()] if "status" not in writable else list(writable.keys())
        values: list[Any] = []
        if "status" not in writable:
            values.append("captured")
        values.extend(writable.values())
        placeholders = ", ".join(["%s"] * len(cols))
        col_clause = ", ".join(cols)
        async with self._require_pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO leads_calls ({col_clause}) VALUES ({placeholders})",  # noqa: S608 — cols whitelisted
                values,
            )
            lead_id = cur.lastrowid
        logger.info("Created standalone lead id={}", lead_id)
        return int(lead_id)

    async def list_leads(
        self, *, limit: int = 100, status: str | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        async with self._require_pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            if status:
                await cur.execute(
                    "SELECT * FROM leads_calls WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                    (status, limit),
                )
            else:
                await cur.execute(
                    "SELECT * FROM leads_calls ORDER BY created_at DESC LIMIT %s", (limit,)
                )
            rows = await cur.fetchall()
        return list(rows)
