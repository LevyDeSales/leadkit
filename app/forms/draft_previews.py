"""A prévia do PDF e da página de resultado do rascunho, com o plano de exemplo ou o do último
teste: o que a operadora vê no editor do PDF antes de salvar e de publicar.

A prévia compila o rascunho com o PDF e a prévia que a página mandou (`drafts.check_templates`),
com as imagens do formulário no conteúdo de agora, sem gravar nada, e monta o que o PDF e a página de resultado recebem de um pedido de verdade: o
plano, o nome da lead, o formulário e as notas, calculadas com as respostas de exemplo ou com as do
teste. O PDF sai do mesmo adaptador do pedido (o WeasyPrint em produção), que só abre arquivos de
`app/assets` e `data:`; a página de resultado é o HTML da prévia, no ambiente isolado.

O "último teste" é o mais recente que terminou com sucesso (`draft_tests.recent`). O plano e as
respostas dele passam pelo modelo do rascunho de agora: se ele mudou desde o teste, a prévia diz
(`PreviewUnavailable`) em vez de mostrar um plano que o modelo não aceita.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.forms import FormSpec
from app.forms.dates import long_date
from app.forms.definition.images import AssetRef
from app.forms.draft_tests import DraftTestResult
from app.forms.drafts import Draft, TemplateTexts, check_templates, templates_document

EXAMPLE = "example"
TEST = "test"
SOURCES = (EXAMPLE, TEST)

NO_TEST = (
    "Ainda não há um teste com sucesso guardado. Rode o teste do rascunho para ver o plano que o "
    "LLM escreve, ou use o plano de exemplo."
)
STALE_PLAN = (
    "O plano do último teste não vale mais com o modelo do plano do rascunho. Rode o teste de novo "
    "ou use o plano de exemplo."
)
STALE_ANSWERS = (
    "As respostas do último teste não valem mais com as perguntas do rascunho. Rode o teste de "
    "novo ou use o plano de exemplo."
)


class PreviewUnavailable(Exception):
    """A prévia não pode usar o plano pedido; a mensagem diz o porquê, sem conteúdo do plano."""


def last_test(tests: Sequence[DraftTestResult]) -> DraftTestResult | None:
    """O teste mais recente que terminou com sucesso e guardou o plano; `tests` vem do mais recente
    para o mais antigo."""
    return next((test for test in tests if test.status == "succeeded" and test.plan), None)


@dataclass(frozen=True)
class TemplatePreview:
    """O que o PDF e a página de resultado recebem: o contrato do rascunho com o PDF e a prévia da
    página, o plano e o contexto do pedido (o do adaptador do PDF)."""

    spec: FormSpec
    plan: dict[str, Any]
    context: dict[str, Any]  # lead, form, scores e generated_at
    source: str  # EXAMPLE ou TEST

    @property
    def generated_date(self) -> str:
        return long_date(self.context["generated_at"])


def preview_of(
    draft: Draft,
    texts: TemplateTexts,
    *,
    source: str,
    tests: Sequence[DraftTestResult],
    now: datetime,
    assets: Mapping[str, AssetRef] | None = None,
) -> TemplatePreview:
    """O rascunho com este PDF e esta prévia, e o plano de `source`. `assets` são as imagens que o
    formulário enxerga, pelo nome: o PDF e a página de resultado mostram as de agora. Levanta
    `DefinitionError` (o PDF ou a prévia não valem, com a linha) ou `PreviewUnavailable` (o plano
    do último teste não existe ou não vale mais)."""
    document = templates_document(texts, draft.document)
    spec = check_templates(
        texts, document, number=draft.next_number, clock=lambda: now, assets=assets
    )
    if source == TEST:
        test = last_test(tests)
        if test is None:
            raise PreviewUnavailable(NO_TEST)
        plan, given = test.plan or {}, test.answers
    else:
        plan, given = spec.example_plan, document["examples"]["answers"]
    try:
        spec.plan_model.model_validate(plan)
    except ValidationError:
        raise PreviewUnavailable(STALE_PLAN) from None
    try:
        answers = spec.answers_model.model_validate(given)
    except ValidationError:
        raise PreviewUnavailable(STALE_ANSWERS) from None
    scored = spec.score(answers) if spec.score is not None else None
    context = {
        "lead": {"name": spec.identity(answers).name},
        "form": {"slug": spec.slug, "title": spec.title},
        "scores": dict(scored.scores) if scored is not None else {},
        "generated_at": now,
    }
    return TemplatePreview(spec=spec, plan=dict(plan), context=context, source=source)
