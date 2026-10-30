"""pump events

Revision ID: b4d2e8f1a6c3
Revises: a1e7c3d9b2f4
Create Date: 2026-10-29 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d2e8f1a6c3'
down_revision: Union[str, Sequence[str], None] = 'a1e7c3d9b2f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('pump_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('boot_id', sa.String(), nullable=True),
    sa.Column('seq', sa.Integer(), nullable=True),
    sa.Column('pump', sa.String(), nullable=False),
    sa.Column('ts', sa.String(length=25), nullable=False),
    sa.Column('on', sa.Boolean(), nullable=False),
    sa.Column('gap', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ux_pump_events_boot_seq', 'pump_events', ['boot_id', 'seq'], unique=True)
    op.create_index('ix_pump_events_pump_ts', 'pump_events', ['pump', 'ts'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('pump_events')
