"""O disparo no Hatchet pelo admin: um módulo só, usado pelas páginas e pela API de admin.

Disparar é síncrono no SDK do Hatchet (`workflows.launch_delivery` e `launch_sync`), e sem engine ele
insiste por minutos. Por isso os disparos correm num executor próprio, pequeno (com o engine fora do
ar cada disparo prende uma thread por minutos), e a resposta **não espera mais que
`HATCHET_TRIGGER_TIMEOUT_SECONDS`**: o disparo já iniciado segue em segundo plano, e o que nem começou
é cancelado.

**Um disparo que não chega ao engine não é erro.** Cada função devolve `True` se o engine recebeu e
`False` se não (o tempo acabou, ou o SDK devolveu `None`): a ação que a precedeu já vale, e o
reconciliador (`app.crons`) dispara o que ficou pendente sem run id. Quem chama diz a diferença a
quem pediu ("enfileirada" ou "adiada para o reconciliador"; `launched` na API).

As tasks do Hatchet recebem e devolvem só ids (`CONTRIBUTING.md`, "Hatchet e dados da lead"), e este
módulo também: nenhuma função aqui conhece dado da lead.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

from app.deps import Deps

# O disparador da tentativa nova (`app.state.trigger`): o do app já tem o limite de tempo dele e devolve
# `None` quando o engine não recebe.
Trigger = Callable[[uuid.UUID, int], Awaitable[str | None]]
# O disparador do teste do rascunho (`app.state.test_trigger`): o mesmo, com o id do teste.
DraftTestTrigger = Callable[[uuid.UUID], Awaitable[str | None]]


@dataclass(frozen=True)
class DraftTestDispatch:
    """Fato explícito de transporte; timeout não prova que nenhum envio aconteceu."""

    state: Literal["enqueued", "not_started", "uncertain"]


# Quantas sincronizações com a planilha um reenvio em lote põe de volta na fila e dispara de uma vez
# (`app.admin.actions.integrations.resend_failed`): o teto da fila de disparos em segundo plano. Quem
# tem mais falhadas que isso repete o reenvio, e cada chamada leva as mais antigas.
RESEND_FAILED_LIMIT = 200

_LAUNCHES = ThreadPoolExecutor(max_workers=4, thread_name_prefix="admin-launch")


async def launch_attempt(trigger: Trigger, job_id: uuid.UUID, attempt: int) -> bool:
    """Dispara a tentativa nova de um pedido (`generate_plan`). `trigger` é o `app.state.trigger`."""
    return bool(await trigger(job_id, attempt))


async def launch_draft_test(test_trigger: DraftTestTrigger, test_id: uuid.UUID) -> bool:
    """Dispara a task `draft_test` do teste do rascunho, que recebe só o id. `test_trigger` é o
    `app.state.test_trigger`. `False`: o engine não recebeu, e quem chama termina o teste como não
    começado (`app.forms.draft_tests.not_started`), em vez de esperar o prazo."""
    return bool(await test_trigger(test_id))


async def launch_delivery(deps: Deps, delivery_id: uuid.UUID) -> bool:
    """Dispara o envio de uma entrega (`deliver`) e espera o engine recebê-lo, até o prazo."""
    # Import aqui: `app.workflows` cria o cliente do Hatchet, que exige o token; importar o admin não
    # deve exigir. Lido do módulo na hora, os testes trocam `launch_delivery`.
    from app import workflows

    return await _within_the_timeout(deps, workflows.launch_delivery, delivery_id)


async def launch_sync(deps: Deps, sync_id: uuid.UUID) -> bool:
    """Dispara a sincronização de um pedido com a planilha (`sync_integration`), como o envio de uma
    entrega."""
    from app import workflows

    return await _within_the_timeout(deps, workflows.launch_sync, sync_id)


async def _within_the_timeout(
    deps: Deps, launch: Callable[[uuid.UUID], str | None], target: uuid.UUID
) -> bool:
    waiting = asyncio.wrap_future(_LAUNCHES.submit(launch, target))
    try:
        run_id = await asyncio.wait_for(
            waiting, timeout=deps.settings.hatchet_trigger_timeout_seconds
        )
    except TimeoutError:
        return False
    return bool(run_id)


def launch_syncs_later(
    sync_ids: Sequence[uuid.UUID], *, first: Future[bool] | None = None
) -> Future[None]:
    """Dispara as sincronizações, uma a uma, **fora da resposta**: devolve na hora, e o `Future` (que
    quem chama pode ignorar) termina quando a fila acaba. Com o engine fora do ar a primeira que falha
    encerra a fila: o reconciliador dispara o resto.

    `first`, se vier, recebe `True` ou `False` assim que a primeira é disparada (o engine a recebeu ou
    não): é como `launch_syncs` sabe se o engine está de pé sem esperar a fila inteira."""
    return _LAUNCHES.submit(_launch_syncs, sync_ids, first)


async def launch_syncs(deps: Deps, sync_ids: Sequence[uuid.UUID]) -> bool:
    """Dispara as sincronizações como `launch_syncs_later` (a mesma fila, na mesma ordem, parando na
    primeira que falha) e **espera só a primeira**, até `HATCHET_TRIGGER_TIMEOUT_SECONDS`: devolve
    `True` se o engine a recebeu, `False` se não (o tempo acabou, ou o SDK devolveu `None`). O resto da
    fila segue em segundo plano, e o que ela não alcançar o reconciliador dispara. Sem sincronização,
    nada é disparado e devolve `False`."""
    if not sync_ids:
        return False
    first: Future[bool] = Future()
    launch_syncs_later(sync_ids, first=first)
    try:
        return await asyncio.wait_for(
            asyncio.wrap_future(first), timeout=deps.settings.hatchet_trigger_timeout_seconds
        )
    except TimeoutError:
        return False


def _launch_syncs(sync_ids: Sequence[uuid.UUID], first: Future[bool] | None = None) -> None:
    try:
        from app import workflows

        for position, sync_id in enumerate(sync_ids):
            reached = workflows.launch_sync(sync_id) is not None
            if position == 0 and first is not None:
                _report(first, reached)
            if not reached:
                return
    except BaseException:
        # Se o laço levantou antes de a primeira ser contada, quem espera não aguarda o prazo inteiro:
        # a resposta é `launched: false`, e o reconciliador dispara o que ficou. Se a primeira já foi
        # contada, este `_report` não muda nada.
        if first is not None:
            _report(first, False)
        raise


def _report(first: Future[bool], reached: bool) -> None:
    """Conta a quem espera se a primeira chegou ao engine. Quem desistiu de esperar (o prazo
    acabou) já cancelou o `Future`: a fila segue sem ele."""
    try:
        first.set_result(reached)
    except InvalidStateError:
        pass


def launch_capture_later(submission_id: uuid.UUID) -> Future[None]:
    """Usa o executor existente; a intenção persistida permite interromper o disparo local."""

    def launch() -> None:
        from app.capture_deliveries import pending_for_submission
        from app.deps import get_deps
        from app.workflows import launch_capture

        deps = get_deps()
        with deps.sessions() as session:
            pending = pending_for_submission(session, submission_id)
        for delivery_id, epoch in pending:
            if launch_capture(delivery_id, epoch) is None:
                break

    return _LAUNCHES.submit(launch)
