"""Copy launch snapshots from the hosted collector database onto this machine.

The research scripts read the local database. This process only copies
`launch_watches`, `launch_snapshots`, and the collector heartbeat. It does not
read or write paper positions, and it does not trade.

    DEXBOT_HOSTED_DATABASE_URL=postgresql+asyncpg://... \\
    DEXBOT_DATABASE_URL=postgresql+asyncpg://dexbot:dexbot@127.0.0.1:5432/dexbot \\
    python -m app.research.sync
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import TradingMode, get_settings
from app.db.models import LaunchSnapshot, LaunchWatch, ProcessHeartbeat
from app.domain.chains import Chain
from app.paper.repository import session_scope

WATCH_FIELDS = (
    "token_address", "quote_address", "symbol", "dex_id", "pool_created_at",
    "first_seen_at", "launch_at", "status", "next_due_at", "snapshots_taken", "last_error",
)
SNAPSHOT_FIELDS = (
    "chain", "due_at", "observed_at", "missed", "not_found", "source",
    "price_usd", "liquidity_usd", "fdv_usd", "market_cap_usd", "base_reserve", "quote_reserve",
    "volume_m5", "volume_h1", "volume_h24", "buys_m5", "sells_m5", "buys_h1", "sells_h1",
    "buyers_m5", "sellers_m5", "buyers_h1", "sellers_h1",
    "screen_allowed", "screen_reason", "screen_checks",
)


@dataclass(frozen=True, slots=True)
class WatchRow:
    chain: str
    pool_address: str
    fields: dict


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    chain: str
    pool_address: str
    age_minutes: int
    fields: dict


@dataclass(frozen=True, slots=True)
class LaunchExport:
    watches: tuple[WatchRow, ...]
    snapshots: tuple[SnapshotRow, ...]


def _chain_value(chain: Chain | str) -> str:
    return chain.value if isinstance(chain, Chain) else str(chain)


def _watch_row(watch: LaunchWatch) -> WatchRow:
    return WatchRow(
        chain=_chain_value(watch.chain),
        pool_address=watch.pool_address,
        fields={name: getattr(watch, name) for name in WATCH_FIELDS},
    )


def _snapshot_row(watch: LaunchWatch, snap: LaunchSnapshot) -> SnapshotRow:
    return SnapshotRow(
        chain=_chain_value(watch.chain),
        pool_address=watch.pool_address,
        age_minutes=snap.age_minutes,
        fields={name: getattr(snap, name) for name in SNAPSHOT_FIELDS},
    )


async def read_launches(session: AsyncSession) -> LaunchExport:
    watches = list(await session.scalars(select(LaunchWatch)))
    by_id = {watch.id: watch for watch in watches}
    snapshots = []
    rows = list(await session.scalars(select(LaunchSnapshot)))
    for snap in rows:
        watch = by_id.get(snap.watch_id)
        if watch is None:
            continue
        snapshots.append(_snapshot_row(watch, snap))
    return LaunchExport(tuple(_watch_row(watch) for watch in watches), tuple(snapshots))


async def write_launches(session: AsyncSession, payload: LaunchExport) -> dict:
    """Upsert by pool, then by slot. Hosted rows replace local rows for the same pool.

    A local snapshot whose slot is not on the host is deleted, so a gap that the
    hosted collector stored as missed cannot survive here as a price.
    """
    existing = list(await session.scalars(select(LaunchWatch)))
    by_key = {(_chain_value(watch.chain), watch.pool_address): watch for watch in existing}
    inserted = updated = 0
    for row in payload.watches:
        watch = by_key.get((row.chain, row.pool_address))
        if watch is None:
            watch = LaunchWatch(chain=Chain(row.chain), pool_address=row.pool_address, **row.fields)
            session.add(watch)
            by_key[(row.chain, row.pool_address)] = watch
            inserted += 1
        else:
            for name, value in row.fields.items():
                setattr(watch, name, value)
            updated += 1
    await session.flush()

    hosted_ages = {by_key[(row.chain, row.pool_address)].id: set() for row in payload.watches}
    upserted = 0
    for row in payload.snapshots:
        watch = by_key[(row.chain, row.pool_address)]
        hosted_ages[watch.id].add(row.age_minutes)
        current = await session.scalar(
            select(LaunchSnapshot).where(
                LaunchSnapshot.watch_id == watch.id, LaunchSnapshot.age_minutes == row.age_minutes,
            )
        )
        if current is None:
            session.add(LaunchSnapshot(watch_id=watch.id, age_minutes=row.age_minutes, **row.fields))
        else:
            for name, value in row.fields.items():
                setattr(current, name, value)
        upserted += 1
    await session.flush()

    removed = 0
    for watch_id, ages in hosted_ages.items():
        local = list(await session.scalars(select(LaunchSnapshot).where(LaunchSnapshot.watch_id == watch_id)))
        for snap in local:
            if snap.age_minutes not in ages:
                await session.delete(snap)
                removed += 1
    return {
        "watches": len(payload.watches),
        "snapshots": len(payload.snapshots),
        "watches_inserted": inserted,
        "watches_updated": updated,
        "snapshots_upserted": upserted,
        "snapshots_removed": removed,
    }


async def read_collector_heartbeat(session: AsyncSession) -> dict | None:
    row = await session.get(ProcessHeartbeat, "collector")
    if row is None or row.last_success_at is None:
        return None
    return {
        "last_success_at": row.last_success_at,
        "last_attempt_at": row.last_attempt_at,
        "last_error": row.last_error,
        "consecutive_failures": row.consecutive_failures,
    }


async def write_collector_heartbeat(session: AsyncSession, beat: dict | None) -> None:
    if beat is None:
        return
    row = await session.get(ProcessHeartbeat, "collector")
    if row is None:
        row = ProcessHeartbeat(component="collector", consecutive_failures=0)
        session.add(row)
    row.last_success_at = beat.get("last_success_at")
    row.last_attempt_at = beat.get("last_attempt_at")
    row.last_error = beat.get("last_error")
    row.consecutive_failures = int(beat.get("consecutive_failures") or 0)
    row.updated_at = datetime.now(timezone.utc)


async def sync_launches(source_url: str, dest_url: str, *, dry_run: bool = False) -> dict:
    if _same_database(source_url, dest_url):
        raise SystemExit("hosted and local database URLs are the same; refusing to copy a database onto itself")
    async with session_scope(source_url) as source:
        payload = await read_launches(source)
        beat = await read_collector_heartbeat(source)
    result = {
        "watches": len(payload.watches),
        "snapshots": len(payload.snapshots),
        "collector_last_success_at": beat["last_success_at"].isoformat() if beat and beat.get("last_success_at") else None,
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    async with session_scope(dest_url) as dest:
        async with dest.begin():
            result.update(await write_launches(dest, payload))
            await write_collector_heartbeat(dest, beat)
    return result


def _same_database(left: str, right: str) -> bool:
    from sqlalchemy.engine import make_url
    from app.db.session import split_database_url

    def identity(url: str) -> tuple:
        parsed = make_url(split_database_url(url)[0])
        return (parsed.host, parsed.port or 5432, parsed.database, parsed.username)

    return identity(left) == identity(right)


def _print(result: dict) -> None:
    when = result.get("collector_last_success_at") or "none"
    print(f"hosted collector last success: {when}")
    print(f"watches: {result.get('watches', 0)}")
    print(f"snapshots: {result.get('snapshots', 0)}")
    if result.get("dry_run"):
        print("dry run: local database was not changed")
        return
    print(
        f"applied: {result.get('watches_inserted', 0)} new watches, "
        f"{result.get('watches_updated', 0)} updated, "
        f"{result.get('snapshots_upserted', 0)} snapshot slots, "
        f"{result.get('snapshots_removed', 0)} local slots removed"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Copy hosted launch snapshots into the local database. Does not trade.")
    parser.add_argument("--dry-run", action="store_true", help="print what the hosted database holds and do not write locally")
    args = parser.parse_args(argv)
    settings = get_settings()
    if settings.trading_mode is not TradingMode.PAPER:
        raise SystemExit("refusing to sync: trading mode is not paper")
    if not settings.hosted_database_url:
        raise SystemExit("Set DEXBOT_HOSTED_DATABASE_URL to the hosted Postgres URL. It is not stored in the repo.")
    result = asyncio.run(sync_launches(settings.hosted_database_url, settings.database_url, dry_run=args.dry_run))
    _print(result)


if __name__ == "__main__":
    main()
