"""Last successful cycle time for the paper runner and the launch collector."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import ProcessHeartbeat
from app.logging import get_logger
from app.paper.repository import session_scope

log = get_logger(__name__)


async def write_heartbeat(component: str, *, success: bool, error: str | None = None) -> None:
    settings = get_settings()

    async def _write() -> None:
        async with session_scope(settings.database_url) as session:
            async with session.begin():
                row = await session.get(ProcessHeartbeat, component)
                now = datetime.now(timezone.utc)
                if row is None:
                    row = ProcessHeartbeat(component=component, consecutive_failures=0)
                    session.add(row)
                row.last_attempt_at = now
                row.updated_at = now
                if success:
                    row.last_success_at = now
                    row.last_error = None
                    row.consecutive_failures = 0
                else:
                    row.last_error = (error or "failed")[:300]
                    row.consecutive_failures = int(row.consecutive_failures or 0) + 1

    try:
        await asyncio.wait_for(_write(), timeout=8)
    except Exception:
        log.warning("heartbeat_not_written", component=component)


async def read_heartbeats(session) -> list[dict]:
    rows = list(await session.scalars(select(ProcessHeartbeat)))
    out = []
    for row in rows:
        out.append({
            "component": row.component,
            "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
            "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else None,
            "last_error": row.last_error,
            "consecutive_failures": row.consecutive_failures,
        })
    return out
