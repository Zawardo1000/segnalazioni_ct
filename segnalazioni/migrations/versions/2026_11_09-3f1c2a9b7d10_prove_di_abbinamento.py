"""prove di abbinamento

Revision ID: 3f1c2a9b7d10
Revises:
Create Date: 2026-11-09 12:00:00.000000

"""
from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import segnalazioni.db

# revision identifiers, used by Alembic.
revision: str = "3f1c2a9b7d10"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostGIS: sul server lo attiva il superuser alla creazione del database (l'utente dell'app non può), e qui
    # IF NOT EXISTS non fa niente. Sul PC e nei test l'utente è superuser e lo crea.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "prove",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("creata", segnalazioni.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Double(), nullable=False),
        sa.Column("lon", sa.Double(), nullable=False),
        sa.Column("precisione_m", sa.Double(), nullable=True),
        sa.Column("origine", sa.String(length=10), nullable=False),
        sa.Column(
            "punto",
            geoalchemy2.types.Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("esito_calcolo", sa.String(length=20), nullable=False),
        sa.Column("ente_calcolato", sa.String(length=200), nullable=True),
        sa.Column("risultato", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fonti", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("esito", sa.String(length=10), nullable=True),
        sa.Column("osm_way_scelto", sa.BigInteger(), nullable=True),
        sa.Column("ente_corretto", sa.String(length=200), nullable=True),
        sa.Column("nota", sa.Text(), nullable=True),
        sa.Column("esito_il", segnalazioni.db.UTCDateTime(timezone=True), nullable=True),
        sa.CheckConstraint("esito IN ('giusto', 'sbagliato')", name=op.f("ck_prove_esito")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prove")),
    )
    op.create_index("ix_prove_creata", "prove", ["creata"], unique=False)
    op.create_index("ix_prove_punto", "prove", ["punto"], unique=False, postgresql_using="gist")


def downgrade() -> None:
    # PostGIS resta: l'utente dell'app sul server non potrebbe toglierlo
    op.drop_index("ix_prove_punto", table_name="prove", postgresql_using="gist")
    op.drop_index("ix_prove_creata", table_name="prove")
    op.drop_table("prove")
