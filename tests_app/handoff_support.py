"""Isolamento do singleton operacional nos cenários sintéticos da capacidade 2."""

import pytest
import sqlalchemy as sa

from app.models import DeploymentContract


@pytest.fixture(autouse=True)
def isolated_handoff_contract(deps):
    """Restaura o estado observado sem mudar o reset compartilhado da suíte."""
    table = DeploymentContract.__table__
    with deps.sessions() as session:
        state = dict(session.execute(sa.select(table).where(table.c.id == 1)).one()._mapping)
    yield
    with deps.sessions.begin() as session:
        session.execute(sa.update(table).where(table.c.id == 1).values(**state))
