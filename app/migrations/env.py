"""Entrada obrigatória de migrations Leadkit: guard online inclusive em upgrade no-op."""

from __future__ import annotations

import importlib.util
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from alembic.script import ScriptDirectory

from app.db import make_engine
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("defina DATABASE_URL para rodar as migrations")
    directory = Path(__file__).resolve().parent
    # O env é carregado por caminho pelo Alembic; seu guard deve vir da MESMA árvore.
    spec = importlib.util.spec_from_file_location(
        "leadkit_compatibility", directory / "compatibility.py"
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    contracts = guard.load_contracts(directory)
    scripts = ScriptDirectory.from_config(config)
    if any(revision.revision not in contracts for revision in scripts.walk_revisions()):
        raise RuntimeError("revisão incompatível; falta contrato estrutural nesta distribuição")
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            # Evita resolução de nomes contra schemas de outra instalação e serializa migradores.
            connection.exec_driver_sql("SET LOCAL search_path TO public, pg_catalog")
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(7234012)")
            guard.validate_database(connection, contracts)
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("a ponte exige preflight online; geração offline de SQL foi desabilitada")
run_migrations_online()
