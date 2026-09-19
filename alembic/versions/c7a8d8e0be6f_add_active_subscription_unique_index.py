"""add_active_subscription_unique_index

Revision ID: c7a8d8e0be6f
Revises: b6e3bfc0bc5e
Create Date: 2026-09-19 17:52:00.000000

"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session

# revision identifiers, used by Alembic.
revision: str = 'c7a8d8e0be6f'
down_revision: Union[str, None] = 'b6e3bfc0bc5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Pre-flight check: ensure no users currently violate the constraint
    bind = op.get_bind()
    session = Session(bind=bind)
    
    # Check for duplicates using raw SQL
    result = bind.execute(sa.text("""
        SELECT user_id, COUNT(*) as cnt 
        FROM subscriptions 
        WHERE status = 'ACTIVE' 
        GROUP BY user_id 
        HAVING COUNT(*) > 1
    """))
    duplicates = result.fetchall()
    
    if duplicates:
        error_msg = "Data conflict: Found users with multiple ACTIVE subscriptions. Cannot apply unique index. Conflicting users: "
        user_ids = [str(row[0]) for row in duplicates]
        error_msg += ", ".join(user_ids)
        raise RuntimeError(error_msg)

    # 2. Add the partial unique index
    op.create_index(
        'ix_uq_active_subscription', 
        'subscriptions', 
        ['user_id'], 
        unique=True, 
        postgresql_where=sa.text("status = 'ACTIVE'")
    )


def downgrade() -> None:
    op.drop_index('ix_uq_active_subscription', table_name='subscriptions', postgresql_where=sa.text("status = 'ACTIVE'"))
