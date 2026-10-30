"""alerts

Revision ID: a1e7c3d9b2f4
Revises: c7f99f1c1ca5
Create Date: 2026-10-29 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1e7c3d9b2f4'
down_revision: Union[str, Sequence[str], None] = 'c7f99f1c1ca5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('app_settings',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value_json', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('subject', sa.String(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('started_ts', sa.String(length=25), nullable=False),
    sa.Column('cleared_ts', sa.String(length=25), nullable=True),
    sa.Column('start_sent', sa.Boolean(), nullable=False),
    sa.Column('clear_sent', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('alerts')
    op.drop_table('app_settings')
