"""boiler

Revision ID: d9a4f2c6e8b1
Revises: b4d2e8f1a6c3
Create Date: 2026-11-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9a4f2c6e8b1'
down_revision: Union[str, Sequence[str], None] = 'b4d2e8f1a6c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.String(length=25)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('boiler_samples',
    sa.Column('ts', TS, nullable=False),
    sa.Column('samples', sa.Integer(), nullable=False),
    *[sa.Column(c, sa.Float(), nullable=True) for c in (
        'outlet_f', 'inlet_f', 'system_f', 'flue_f', 'tank_f', 'outdoor_f', 'target_f', 'sh_setpoint_f',
        'dhw_setpoint_f', 'rate_pct', 'fan_rpm', 'pump_pct')],
    sa.Column('state', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(), nullable=False),
    sa.Column('flame_s', sa.Float(), nullable=False),
    sa.Column('btu', sa.Float(), nullable=False),
    sa.Column('sh_demand', sa.Boolean(), nullable=False),
    sa.Column('dhw_demand', sa.Boolean(), nullable=False),
    sa.Column('raw_json', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('ts')
    )
    op.create_table('boiler_cycles',
    sa.Column('start_ts', TS, nullable=False),
    sa.Column('end_ts', TS, nullable=False),
    sa.Column('mode', sa.String(), nullable=False),
    sa.Column('end_reason', sa.String(), nullable=False),
    sa.Column('partial', sa.Boolean(), nullable=False),
    sa.Column('avg_rate_pct', sa.Float(), nullable=True),
    sa.Column('min_fire_s', sa.Float(), nullable=False),
    sa.Column('btu', sa.Float(), nullable=False),
    sa.Column('max_outlet_f', sa.Float(), nullable=True),
    sa.Column('avg_inlet_f', sa.Float(), nullable=True),
    sa.Column('avg_flue_f', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('start_ts')
    )
    op.create_table('boiler_params',
    sa.Column('ts', TS, nullable=False),
    sa.Column('values_json', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('ts')
    )
    op.create_table('boiler_counters',
    sa.Column('ts', TS, nullable=False),
    *[sa.Column(c, sa.Integer(), nullable=False) for c in (
        'power_on_h', 'burner_starts', 'ignitions', 'burner_h', 'burner_h_2', 'sh_cycles', 'dhw_cycles')],
    sa.PrimaryKeyConstraint('ts')
    )


def downgrade() -> None:
    """Downgrade schema."""
    for t in ('boiler_counters', 'boiler_params', 'boiler_cycles', 'boiler_samples'):
        op.drop_table(t)
