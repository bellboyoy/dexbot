"""widen pool ids for 32-byte identifiers and add process heartbeats

Revision ID: e8c3d5f02b21
Revises: d7b2c4e91a10
Create Date: 2026-11-10 13:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e8c3d5f02b21"
down_revision: Union[str, Sequence[str], None] = "d7b2c4e91a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("pools", "address", existing_type=sa.String(length=64), type_=sa.String(length=80))
    op.alter_column("launch_watches", "pool_address", existing_type=sa.String(length=64), type_=sa.String(length=80))
    op.create_table(
        "process_heartbeats",
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("component"),
    )


def downgrade() -> None:
    op.drop_table("process_heartbeats")
    op.alter_column("launch_watches", "pool_address", existing_type=sa.String(length=80), type_=sa.String(length=64))
    op.alter_column("pools", "address", existing_type=sa.String(length=80), type_=sa.String(length=64))
