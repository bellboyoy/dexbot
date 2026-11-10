"""point-in-time launch watches and snapshots

Revision ID: d7b2c4e91a10
Revises: c4d8e1a2b3f0
Create Date: 2026-11-09 23:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d7b2c4e91a10"
down_revision: Union[str, Sequence[str], None] = "c4d8e1a2b3f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHAIN = postgresql.ENUM("solana", "base", "bsc", "arbitrum", name="chain", create_type=False)
USD = sa.Numeric(precision=38, scale=18)


def upgrade() -> None:
    op.create_table(
        "launch_watches",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("chain", CHAIN, nullable=False),
        sa.Column("pool_address", sa.String(length=64), nullable=False),
        sa.Column("token_address", sa.String(length=64), nullable=False),
        sa.Column("quote_address", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("dex_id", sa.String(length=64), nullable=True),
        sa.Column("pool_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("launch_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshots_taken", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chain", "pool_address"),
    )
    op.create_index("ix_launch_watches_due", "launch_watches", ["status", "next_due_at"], unique=False)
    op.create_table(
        "launch_snapshots",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("watch_id", sa.BigInteger(), nullable=False),
        sa.Column("chain", CHAIN, nullable=False),
        sa.Column("age_minutes", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missed", sa.Boolean(), nullable=False),
        sa.Column("not_found", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("price_usd", USD, nullable=True),
        sa.Column("liquidity_usd", USD, nullable=True),
        sa.Column("fdv_usd", USD, nullable=True),
        sa.Column("market_cap_usd", USD, nullable=True),
        sa.Column("base_reserve", USD, nullable=True),
        sa.Column("quote_reserve", USD, nullable=True),
        sa.Column("volume_m5", USD, nullable=True),
        sa.Column("volume_h1", USD, nullable=True),
        sa.Column("volume_h24", USD, nullable=True),
        sa.Column("buys_m5", sa.Integer(), nullable=True),
        sa.Column("sells_m5", sa.Integer(), nullable=True),
        sa.Column("buys_h1", sa.Integer(), nullable=True),
        sa.Column("sells_h1", sa.Integer(), nullable=True),
        sa.Column("buyers_m5", sa.Integer(), nullable=True),
        sa.Column("sellers_m5", sa.Integer(), nullable=True),
        sa.Column("buyers_h1", sa.Integer(), nullable=True),
        sa.Column("sellers_h1", sa.Integer(), nullable=True),
        sa.Column("screen_allowed", sa.Boolean(), nullable=True),
        sa.Column("screen_reason", sa.Text(), nullable=True),
        sa.Column("screen_checks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["watch_id"], ["launch_watches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("watch_id", "age_minutes"),
    )
    op.create_index("ix_launch_snapshots_watch", "launch_snapshots", ["watch_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_launch_snapshots_watch", table_name="launch_snapshots")
    op.drop_table("launch_snapshots")
    op.drop_index("ix_launch_watches_due", table_name="launch_watches")
    op.drop_table("launch_watches")
