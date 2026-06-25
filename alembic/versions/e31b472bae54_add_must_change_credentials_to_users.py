"""add must_change_credentials to users

Revision ID: e31b472bae54
Revises: 4a2187d3a01d
Create Date: 2026-06-23 18:42:06.763292
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e31b472bae54'
down_revision: Union[str, None] = '4a2187d3a01d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    op.add_column(
        'users',
        sa.Column(
            'must_change_credentials',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('0')
        )
    )

    # Force existing admins to update credentials
    op.execute("""
        UPDATE users
        SET must_change_credentials = 1
        WHERE role = 'ADMIN'
    """)


def downgrade() -> None:

    op.drop_column(
        'users',
        'must_change_credentials'
    )