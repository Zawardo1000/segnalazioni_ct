"""codice per l'esito delle prove

Revision ID: 7a2e91c4d5b3
Revises: 3f1c2a9b7d10
Create Date: 2026-11-09 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a2e91c4d5b3"
down_revision: Union[str, Sequence[str], None] = "3f1c2a9b7d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # le migrazioni aggiungono: colonna facoltativa, le prove di prima restano senza codice (esito non più modificabile)
    op.add_column("prove", sa.Column("token_esito", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("prove", "token_esito")
