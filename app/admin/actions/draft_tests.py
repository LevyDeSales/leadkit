"""As ações do admin sobre o teste do rascunho: o que a página do teste junta (`testing`: o prompt
montado ou a pontuação com as respostas pedidas, os problemas e os testes recentes), a busca de um
teste (`find`), o PDF dele (`pdf`) e iniciar um teste (`start`).

A página e a API chamam as mesmas funções (ADR 0015, "A página e a API dividem os serviços"): elas não
conhecem `Request`, HTML nem aviso de tela. Recebem o autor (`actor`: `ADMIN_USERNAME` pela página,
`chave:<nome>` pela API) e as respostas do teste (texto JSON do campo da página, ou objeto da API),
devolvem um resultado ou levantam uma exceção de domínio, e cada lado a traduz. A recusa do teste
(`DraftTestRefused`, de `app.forms.draft_tests`) é a mesma nas duas: a página a mostra como 409, ou 429
com `Retry-After` na pausa; a API, como `409 state_conflict` ou `429 test_paused`
(`DraftTestRefused.paused` diz qual). `testing`, `find` e `pdf` só leem: não gravam, não auditam e não
chamam o LLM.

**Cada teste é uma chamada paga ao LLM.** Iniciar põe o teste na fila e dispara a task `draft_test`
do Hatchet (`app.admin.launching`), que recebe só o id do teste. Se o engine não recebe o disparo, o
teste termina na hora como não começado (`draft_tests.not_started`), sem pausa para o seguinte, e
`launched` diz `False`. As respostas de um teste são fictícias (as da operadora, não de uma lead),
mas nada aqui as leva ao log, e as mensagens de erro nunca citam o que foi digitado.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi.concurrency import run_in_threadpool

from app.admin import launching
from app.deps import Deps
from app.forms import UnknownForm, draft_tests, drafts
from app.forms.definition import DefinitionError, Problem

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Testing:
    """O que a página do teste do rascunho junta: o rascunho, o prompt montado (`assembled`) ou a
    pontuação (`scored`) com as respostas pedidas, os problemas que impediram um dos dois e os
    testes recentes. Nunca tem `assembled` e `scored` juntos; com `problems`, não tem nenhum."""

    draft: drafts.Draft
    assembled: drafts.Assembled | None
    scored: drafts.Scored | None
    problems: tuple[Problem, ...]
    recent: list[draft_tests.DraftTestResult]


def testing(
    deps: Deps,
    slug: str,
    *,
    answers: str | Mapping[str, Any] | None = None,
    day: str = "",
    scoring: bool = False,
    now: datetime,
) -> Testing:
    """O estado do teste do rascunho de `slug`: o prompt montado com as `answers` (as de exemplo, se
    `None`; texto JSON da página ou objeto da API) e a data simulada `day` ou, com `scoring`, a
    pontuação delas, mais os testes recentes. **Só lê:** não grava, não audita e não chama o LLM.

    Respostas, data ou rascunho que não valem não são uma exceção: voltam em `problems` (sem repetir
    o valor), e os testes recentes saem do mesmo jeito. Levanta `UnknownForm`."""
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    if draft is None:
        raise UnknownForm(slug)
    assembled = scored = None
    problems: tuple[Problem, ...] = ()
    try:
        if scoring:
            scored = drafts.score(draft, answers=answers, now=now)
        else:
            assembled = drafts.assemble(draft, answers=answers, day=day, now=now)
    except DefinitionError as refused:
        problems = tuple(refused.problems)
    with deps.sessions() as session:
        recent = draft_tests.recent(session, draft, now=now)
    return Testing(
        draft=draft, assembled=assembled, scored=scored, problems=problems, recent=recent
    )


class UnknownTest(Exception):
    """O teste do caminho não é um teste deste formulário: o id não é um UUID, nunca existiu, é de
    outro formulário, ou já saiu (só os mais recentes ficam, e a retenção apaga os de poucos dias)."""


class NoPdf(Exception):
    """O teste existe, mas não chegou ao PDF (na fila, rodando ou falhou antes dele)."""


@dataclass(frozen=True)
class Found:
    """Um teste do rascunho e o rascunho do formulário dele."""

    draft: drafts.Draft
    result: draft_tests.DraftTestResult


@dataclass(frozen=True)
class DraftTestPdf:
    """O PDF que um teste gerou, com o nome do arquivo."""

    filename: str
    content: bytes


def find(deps: Deps, slug: str, test_id: str, *, now: datetime) -> Found:
    """O teste `test_id` (o id como veio no caminho, em texto) do formulário `slug`, com o estado
    de agora: um que passou do prazo sem terminar sai como `interrupted`. **Só lê.**

    Levanta `UnknownForm` e `UnknownTest`."""
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    if draft is None:
        raise UnknownForm(slug)
    try:
        wanted = uuid.UUID(test_id)
    except ValueError:
        raise UnknownTest from None
    with deps.sessions() as session:
        result = draft_tests.find(session, draft, wanted, now=now)
    if result is None:
        raise UnknownTest
    return Found(draft=draft, result=result)


def pdf(deps: Deps, slug: str, test_id: str, *, now: datetime) -> DraftTestPdf:
    """O PDF do teste `test_id` do formulário `slug`, com o nome `teste-<slug>-<AAAAMMDD-HHMM>.pdf`
    (o minuto em que o teste começou). Os dados do teste são os de exemplo da operadora, não de uma
    lead. **Só lê.**

    Levanta `UnknownForm`, `UnknownTest` e `NoPdf`."""
    run = find(deps, slug, test_id, now=now)
    with deps.sessions() as session:
        content = draft_tests.pdf_of(session, run.draft, run.result.id)
    if content is None:
        raise NoPdf
    return DraftTestPdf(
        filename=f"teste-{slug}-{run.result.started_at:%Y%m%d-%H%M}.pdf", content=content
    )


@dataclass(frozen=True)
class Started:
    """O teste que entrou na fila. `launched`: o engine recebeu o disparo (`False`: o teste terminou
    como não começado, e a pessoa pode tentar de novo)."""

    test_id: uuid.UUID
    launched: bool


async def start(
    deps: Deps,
    test_trigger: launching.DraftTestTrigger,
    slug: str,
    *,
    actor: str,
    answers: str | Mapping[str, Any] | None,
    day: str,
    now: datetime,
) -> Started:
    """Põe na fila o teste do rascunho salvo do formulário `slug`, em nome de `actor`, e dispara a
    task `draft_test` (`test_trigger` é o `app.state.test_trigger`). `answers` são as respostas
    (texto JSON da página ou objeto da API; `None`, as de exemplo do rascunho) e `day`, a data
    simulada (`AAAA-MM-DD`, ou vazia).

    Levanta `UnknownForm`, `DefinitionError` (as respostas, a data ou o rascunho que não compila) e
    `DraftTestRefused` (um teste em andamento, ou a pausa depois do último); nada é gravado nem
    disparado nesses casos."""
    test_id = await run_in_threadpool(
        draft_tests.start, deps, slug, actor=actor, answers=answers, day=day, now=now
    )
    launched = await launching.launch_draft_test(test_trigger, test_id)
    if not launched:
        await run_in_threadpool(draft_tests.not_started, deps, test_id)
    logger.info(
        "admin: teste do rascunho slug=%s teste=%s por=%s launched=%s",
        slug,
        test_id,
        actor,
        launched,
    )
    return Started(test_id=test_id, launched=launched)
