"""segnalazioni, enti e gestionale

Revision ID: b41d7e2a9c05
Revises: 7a2e91c4d5b3
Create Date: 2026-11-10 09:00:00.000000

"""
from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b41d7e2a9c05"
down_revision: Union[str, Sequence[str], None] = "7a2e91c4d5b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "enti",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("codice", sa.String(40), nullable=False),
        sa.Column("nome", sa.String(200), nullable=False),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("attivo", sa.Boolean(), nullable=False),
        sa.Column("creato", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_enti")),
        sa.UniqueConstraint("codice", name=op.f("uq_enti_codice")),
    )
    op.create_table(
        "segnalazioni",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("codice", sa.String(30), nullable=False),
        sa.Column("creata", TS, nullable=False),
        sa.Column("aggiornata", TS, nullable=False),
        sa.Column("tipo", sa.String(10), nullable=False),
        sa.Column("tipo_iniziale", sa.String(10), nullable=False),
        sa.Column("nota", sa.Text()),
        sa.Column("lat", sa.Double(), nullable=False),
        sa.Column("lon", sa.Double(), nullable=False),
        sa.Column("precisione_m", sa.Double()),
        sa.Column("origine", sa.String(10), nullable=False),
        sa.Column("punto", geoalchemy2.Geography("POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("comune", sa.String(200)),
        sa.Column("osm_way", sa.BigInteger()),
        sa.Column("strada", sa.String(300)),
        sa.Column("abbinamento", postgresql.JSONB(), nullable=False),
        sa.Column("ente_calcolato", sa.String(40)),
        sa.Column("ente_id", sa.Integer(), sa.ForeignKey("enti.id", name=op.f("fk_segnalazioni_ente_id_enti"))),
        sa.Column("stato", sa.String(20), nullable=False),
        sa.Column("esito", sa.String(20)),
        sa.Column("chiusa_il", TS),
        sa.Column("inoltri", sa.SmallInteger(), nullable=False),
        sa.Column("foto", sa.String(64)),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("dispositivo_hash", sa.String(64)),
        sa.Column("email_inviate", sa.SmallInteger(), nullable=False),
        sa.Column("analisi", postgresql.JSONB()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segnalazioni")),
        sa.UniqueConstraint("codice", name=op.f("uq_segnalazioni_codice")),
        sa.CheckConstraint("tipo IN ('carcassa', 'rifiuto')", name=op.f("ck_segnalazioni_tipo")),
        sa.CheckConstraint("tipo_iniziale IN ('carcassa', 'rifiuto')", name=op.f("ck_segnalazioni_tipo_iniziale")),
        sa.CheckConstraint("stato IN ('nuova', 'presa_in_carico', 'chiusa', 'da_smistare')",
                           name=op.f("ck_segnalazioni_stato")),
        sa.CheckConstraint("esito IN ('rimossa', 'non_trovata', 'doppione')", name=op.f("ck_segnalazioni_esito")),
    )
    op.create_index("ix_segnalazioni_punto", "segnalazioni", ["punto"], postgresql_using="gist")
    op.create_index("ix_segnalazioni_ente_stato", "segnalazioni", ["ente_id", "stato"])
    op.create_index("ix_segnalazioni_dispositivo", "segnalazioni", ["dispositivo_hash"])
    op.create_table(
        "eventi",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("segnalazione_id", sa.BigInteger(),
                  sa.ForeignKey("segnalazioni.id", name=op.f("fk_eventi_segnalazione_id_segnalazioni")),
                  nullable=False),
        sa.Column("quando", TS, nullable=False),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("autore", sa.String(200), nullable=False),
        sa.Column("da_ente_id", sa.Integer(), sa.ForeignKey("enti.id", name=op.f("fk_eventi_da_ente_id_enti"))),
        sa.Column("a_ente_id", sa.Integer(), sa.ForeignKey("enti.id", name=op.f("fk_eventi_a_ente_id_enti"))),
        sa.Column("dettagli", postgresql.JSONB(), nullable=False),
        sa.Column("visibile_cittadino", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eventi")),
    )
    op.create_index("ix_eventi_segnalazione", "eventi", ["segnalazione_id", "quando"])
    op.create_table(
        "sessioni",
        sa.Column("id_hash", sa.String(64), nullable=False),
        sa.Column("creata", TS, nullable=False),
        sa.Column("scade", TS, nullable=False),
        sa.Column("sub", sa.String(100), nullable=False),
        sa.Column("utente", sa.String(200), nullable=False),
        sa.Column("ruolo", sa.String(20), nullable=False),
        sa.Column("ente_id", sa.Integer(), sa.ForeignKey("enti.id", name=op.f("fk_sessioni_ente_id_enti"))),
        sa.Column("csrf", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_sessioni")),
    )
    op.create_table(
        "login_in_corso",
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("creato", TS, nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("verificatore", sa.String(128), nullable=False),
        sa.PrimaryKeyConstraint("state", name=op.f("pk_login_in_corso")),
    )


def downgrade() -> None:
    op.drop_table("login_in_corso")
    op.drop_table("sessioni")
    op.drop_index("ix_eventi_segnalazione", table_name="eventi")
    op.drop_table("eventi")
    op.drop_index("ix_segnalazioni_dispositivo", table_name="segnalazioni")
    op.drop_index("ix_segnalazioni_ente_stato", table_name="segnalazioni")
    op.drop_index("ix_segnalazioni_punto", table_name="segnalazioni")
    op.drop_table("segnalazioni")
    op.drop_table("enti")
