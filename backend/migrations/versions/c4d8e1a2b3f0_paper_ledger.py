"""paper ledger columns and account

Revision ID: c4d8e1a2b3f0
Revises: b2ce3eebdb82
Create Date: 2026-11-09 22:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c4d8e1a2b3f0"
down_revision: Union[str, Sequence[str], None] = "b2ce3eebdb82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("starting_balance_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("cash_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("realized_pnl_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("fees_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("gas_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("slippage_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("day_utc", sa.Date(), nullable=False),
        sa.Column("day_realized_pnl_usd", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("consecutive_losses", sa.Integer(), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kill_switch", sa.Boolean(), nullable=False),
        sa.Column("kill_reason", sa.Text(), nullable=True),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("scan_count", sa.Integer(), nullable=False),
        sa.Column("last_cycle", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.add_column("positions", sa.Column("token_qty", sa.Numeric(precision=38, scale=18), nullable=True))
    op.add_column("positions", sa.Column("mark_price_usd", sa.Numeric(precision=38, scale=18), nullable=True))
    op.add_column("positions", sa.Column("unrealized_pnl_usd", sa.Numeric(precision=38, scale=18), nullable=True))
    op.add_column("positions", sa.Column("entry_liquidity_usd", sa.Numeric(precision=38, scale=18), nullable=True))
    op.add_column("positions", sa.Column("fee_rate", sa.Numeric(precision=12, scale=8), nullable=True))
    op.add_column("trades", sa.Column("slippage_usd", sa.Numeric(precision=38, scale=18), nullable=True))


def downgrade() -> None:
    op.drop_column("trades", "slippage_usd")
    op.drop_column("positions", "fee_rate")
    op.drop_column("positions", "entry_liquidity_usd")
    op.drop_column("positions", "unrealized_pnl_usd")
    op.drop_column("positions", "mark_price_usd")
    op.drop_column("positions", "token_qty")
    op.drop_table("paper_accounts")
