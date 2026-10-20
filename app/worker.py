"""Worker do Hatchet: `python -m app.worker`.

O SDK reimporta este módulo num processo filho: nada de conexão a banco ou rede na importação.
Encerramento: SIGTERM pausa o worker e espera as tasks em andamento; configure o grace period do
container acima da task mais longa (`plan`, até 9 min; o compose usa 10 min) e o restante volta
para a fila sozinho.
"""

from __future__ import annotations

import logging
from datetime import datetime

import sqlalchemy as sa

from app import destinations
from app.crons import reconcile_jobs, retention_purge
from app.deps import Deps, get_deps
from app.forms.data_notices import log_data_notices
from app.hatchet_client import hatchet
from app.integrations import config as integration_config
from app.workflows import deliver, deliver_capture, draft_test, generate_plan, sync_integration

logger = logging.getLogger(__name__)


def ready_deps(now: datetime | None = None) -> Deps:
    """Valida a configuração e a conexão antes de aceitar tasks e prepara o destino do webhook
    (`app.destinations.prepare`: a importação do WEBHOOK_URL na primeira subida e a chave) e os tokens
    das integrações (`app.integrations.config.prepare`). Avisa
    no log dos dados vencidos de um formulário (`app.forms.data_notices`), conferidos no dia de
    `now` (o agora, se não vier). Os avisos são só um aviso: um erro do banco ao calculá-los (sem as
    tabelas, no primeiro deploy da stack do Swarm, que sobe antes das migrations) vira um WARNING e
    o worker segue; a conexão, conferida antes, continua parando a subida."""
    deps = get_deps()
    with deps.sessions() as session:
        session.connection()
    destinations.prepare(deps)
    integration_config.prepare(deps)
    try:
        log_data_notices(deps.sessions, now)
    except sa.exc.SQLAlchemyError:
        logger.warning(
            "avisos dos formulários: o banco não respondeu na subida do worker (antes das "
            "migrations ou fora do ar); o worker sobe sem os avisos",
            exc_info=True,
        )
    return deps


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    deps = ready_deps()
    worker = hatchet.worker(
        "mcc-worker",
        slots=deps.settings.worker_slots,
        workflows=[
            generate_plan,
            deliver,
            deliver_capture,
            sync_integration,
            draft_test,
            reconcile_jobs,
            retention_purge,
        ],
    )
    worker.start()


if __name__ == "__main__":
    main()
