from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    connect_args: dict[str, object] = {}
    if database_url.startswith("postgresql+psycopg"):
        # Compatível com poolers (pgbouncer/Supabase/Neon), que não suportam prepared statements.
        connect_args["prepare_threshold"] = None
    # hide_parameters: um erro do banco vai para o log e, numa task, para o Hatchet; sem os
    # parâmetros, ele não leva dado da lead, o caminho da URL de um destino nem um segredo cifrado.
    return create_engine(
        database_url, pool_pre_ping=True, hide_parameters=True, connect_args=connect_args
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
