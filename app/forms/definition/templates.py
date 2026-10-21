"""Os templates de uma definição compilada, já compilados no ambiente isolado, com o contexto de
cada um. O motor os pede pelo contrato (`FormSpec.templates`): o prompt pelo `render_prompt` do
formulário, o PDF pelo adaptador do WeasyPrint e a prévia pela página de resultado.

Contexto de cada template:
- instruções: `data` (os dados do formulário). Renderizam uma vez, na compilação: são iguais para
  toda lead;
- prompt: `answers` (as respostas validadas), `labels` (o rótulo das opções escolhidas), `scores`
  (a pontuação dessas respostas: as notas e os níveis das faixas, sem a prioridade e a persona),
  `today` (a data de hoje em São Paulo, pelo relógio da compilação), `data` e `phases` (as fases
  com datas, ver `app.forms.definition.phases`; vazio sem `phases` na definição);
- PDF e prévia: `plan` (o plano sem os campos internos), `lead.name`, `form.title`, `form.slug`,
  `scores` (as notas da pontuação calculada no envio que a operadora liberou em `pdf.scores`,
  com o nível e o rótulo da faixa de cada uma; `{}` sem pontuação ou sem nota liberada),
  `generated_date` (a data da geração, por extenso) e `brand` (a biblioteca de componentes da
  marca, `app.forms.definition.components`). A prioridade e a persona não chegam a nenhum.
  As imagens do formulário entram por `asset:<nome>` (`app.forms.definition.images`): o PDF as abre
  do storage pelo buscador do WeasyPrint, e a prévia as troca pelo endereço da rota que as serve.

Na compilação, cada template é conferido: sintaxe, filtros, variáveis do contexto, campos lidos
direto de `answers`, `labels`, `plan`, `lead` e `form` e, por fim, a renderização com as respostas
e o plano de exemplo. Os problemas saem com o caminho do template na definição e a linha.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from app.branding import installation_brand
from app.forms import PlanProjectionError
from app.forms.dates import local_date, long_date
from app.forms.definition.components import COMPONENT_NAMES
from app.forms.definition.data import template_data
from app.forms.definition.document import DefinitionError, FormDocument, Problem
from app.forms.definition.images import AssetRef, rewrite
from app.forms.definition.phases import Calendar, PhaseStatus
from app.forms.definition.questions import CHOICE_KINDS, labels_of, questions_of
from app.forms.definition.scoring import Scoring, ScoringFailure
from app.forms.sandbox import SandboxTemplate, TemplateProblem

INSTRUCTIONS_CONTEXT = frozenset({"data"})
PROMPT_CONTEXT = frozenset({"answers", "labels", "scores", "today", "data", "phases"})
PAGE_CONTEXT = frozenset({"plan", "lead", "form", "scores", "generated_date", "brand"})
LEAD_FIELDS = frozenset({"name"})
FORM_FIELDS = frozenset({"slug", "title"})
NOT_RELEASED = "é uma nota que o PDF e a prévia não podem mostrar; libere-a no editor do PDF"


@dataclass(frozen=True)
class FormTemplates:
    prompt: SandboxTemplate
    pdf: SandboxTemplate
    pdf_css: str
    preview: SandboxTemplate
    plan_model: type[BaseModel]
    labels: Callable[[BaseModel], SimpleNamespace]
    data: Mapping[str, Any]
    clock: Callable[[], datetime]
    scoring: Scoring | None = None
    reads_scores: bool = False  # o prompt lê `scores`; só então a pontuação entra na montagem
    # Os nomes de `scores` que o PDF e a prévia podem receber (`pdf.scores` e as faixas delas).
    released: frozenset[str] = frozenset()
    calendar: Calendar | None = None  # as fases com datas; None sem `phases` na definição
    # As imagens do formulário (`asset:<nome>`) que este contrato usa, pelo nome, com o conteúdo
    # fixado de cada uma. Vazio na compilação: quem monta o contrato as põe com `with_assets`
    # (a versão publicada, com o que fixou na publicação; o rascunho, com as imagens de agora).
    assets: Mapping[str, AssetRef] = dataclass_field(default_factory=dict)
    project_plan: Callable[[Mapping[str, Any], str], dict[str, Any]] | None = None

    def phases(self) -> list[PhaseStatus]:
        """As fases do plano que o prompt recebe agora (a data de hoje em São Paulo, pelo relógio
        do formulário): vazio sem fases com datas."""
        return self.calendar.on(local_date(self.clock())) if self.calendar is not None else []

    def render_prompt(self, answers: BaseModel) -> str:
        """O prompt montado com as respostas validadas. Levanta `TemplateProblem`, também se o
        prompt lê `scores` e a pontuação não calcula com estas respostas."""
        scores: Mapping[str, Any] = {}
        if self.scoring is not None and self.reads_scores:
            try:
                scores = self.scoring.run(answers).result.scores
            except ScoringFailure:
                # Sem o motivo nem as respostas: a mensagem vai ao log e ao teste do rascunho.
                raise TemplateProblem(
                    self.prompt.name, None, "a pontuação não pôde ser calculada com estas respostas"
                ) from None
        return self.prompt.render(
            {
                "answers": SimpleNamespace(**answers.model_dump()),
                "labels": self.labels(answers),
                "scores": SimpleNamespace(**scores),
                "today": local_date(self.clock()),
                "data": self.data,
                "phases": self.phases(),
            }
        )

    def render_pdf(
        self,
        plan: BaseModel,
        *,
        lead: Mapping[str, Any],
        form: Mapping[str, Any],
        scores: Mapping[str, Any],
        generated_date: str,
    ) -> str:
        """O HTML do PDF do plano validado. Levanta `TemplateProblem`."""
        return self.pdf.render(
            self._page(
                plan.model_dump(mode="python", warnings=False),
                lead,
                form,
                scores,
                generated_date,
                name=self.pdf.name,
            )
        )

    def render_preview(
        self,
        document: Mapping[str, Any],
        *,
        lead: Mapping[str, Any],
        form: Mapping[str, Any],
        scores: Mapping[str, Any],
        generated_date: str,
    ) -> str:
        """O HTML da prévia do plano gravado, validado de novo pelo modelo da mesma versão.
        Levanta `TemplateProblem`, também se o plano não valida (sem os valores dele)."""
        html = self.preview.render(
            self._page(document, lead, form, scores, generated_date, name=self.preview.name)
        )
        return rewrite(html, self.assets)  # a página roda no navegador da lead: imagem é uma rota

    def _page(
        self,
        document: Mapping[str, Any],
        lead: Mapping[str, Any],
        form: Mapping[str, Any],
        scores: Mapping[str, Any],
        generated_date: str,
        *,
        name: str,
    ) -> dict[str, Any]:
        """O contexto do PDF e da prévia: o plano sem os campos internos, da lead e do
        formulário só o que o contrato do template promete e, da pontuação, só as notas liberadas
        para o PDF."""
        try:
            if self.project_plan is None:
                raise PlanProjectionError("plan_projection_unavailable")
            projected = self.project_plan(document, "participant")
        except PlanProjectionError:
            raise TemplateProblem(
                name, None, "o plano gravado não valida com o modelo do plano"
            ) from None
        return {
            "plan": _namespace(projected),
            "lead": SimpleNamespace(name=lead.get("name")),
            "form": SimpleNamespace(slug=form.get("slug"), title=form.get("title")),
            "scores": {name: value for name, value in scores.items() if name in self.released},
            "generated_date": generated_date,
            "brand": installation_brand().components(),
        }


def _namespace(value: Any) -> Any:
    """Adapta somente a cópia projetada ao acesso por atributo dos templates existentes."""
    if isinstance(value, dict):
        return SimpleNamespace(**{name: _namespace(item) for name, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


# --- compilação -----------------------------------------------------------------------------


def _field_checks(
    definition: FormDocument, scoring: Scoring | None, released: frozenset[str] | None = None
) -> dict[str, Callable[[str], str | None]]:
    """Para cada variável de esquema fixo, o que há de errado num campo lido direto dela. No PDF
    e na prévia (`released` dado), `scores` só tem as notas liberadas."""
    names = {question.name for question in questions_of(definition.steps)}
    choices = {q.name for q in questions_of(definition.steps) if q.kind in CHOICE_KINDS}
    internal = {field.name for field in definition.plan.fields if field.internal}
    visible = {field.name for field in definition.plan.fields} - internal

    def answer(field: str) -> str | None:
        return None if field in names else "não é uma pergunta do formulário"

    def label(field: str) -> str | None:
        return None if field in choices else "não é uma pergunta de escolha"

    def plan(field: str) -> str | None:
        if field in internal:
            return "é um campo interno, que não chega ao PDF nem à prévia"
        return None if field in visible else "não é um campo do plano"

    def lead(field: str) -> str | None:
        return None if field in LEAD_FIELDS else "não existe no contexto do template"

    def form(field: str) -> str | None:
        return None if field in FORM_FIELDS else "não existe no contexto do template"

    def component(field: str) -> str | None:
        return None if field in COMPONENT_NAMES else "não é um componente da marca"

    scored = set(scoring.names) if scoring is not None else set()

    def score(field: str) -> str | None:
        if released is not None and field in scored and field not in released:
            return NOT_RELEASED
        if field in scored:
            return None
        return "não é uma nota da pontuação" if scored else "o formulário não tem pontuação"

    return {
        "answers": answer,
        "labels": label,
        "plan": plan,
        "lead": lead,
        "form": form,
        "scores": score,
        "brand": component,
    }


def _references(
    template: SandboxTemplate,
    context: frozenset[str],
    checks: Mapping[str, Callable[[str], str | None]],
) -> list[Problem]:
    """Variáveis fora do contexto do template e campos que não existem nas de esquema fixo."""
    problems = [
        Problem(template.name, f"variável inexistente: {name}", line)
        for name, line in template.undeclared().items()
        if name not in context
    ]
    for variable in sorted(context & checks.keys()):
        for field, line in template.fields_of(variable):
            message = checks[variable](field)
            if message is not None:
                problems.append(Problem(template.name, f"{variable}.{field} {message}", line))
    return sorted(problems, key=lambda problem: problem.line or 0)


def released_problems(definition: FormDocument) -> list[Problem]:
    """As notas liberadas para o PDF (`pdf.scores`) que não existem na pontuação, ou que se
    repetem."""
    names = [score.name for score in definition.scoring.scores] if definition.scoring else []
    problems: list[Problem] = []
    for index, name in enumerate(definition.pdf.scores):
        path = f"pdf.scores.{index}"
        if name in definition.pdf.scores[:index]:
            problems.append(Problem(path, "nota repetida"))
        elif not names:
            problems.append(Problem(path, "o formulário não tem pontuação"))
        elif name not in names:
            problems.append(Problem(path, "não é uma nota da pontuação"))
    return problems


@dataclass(frozen=True)
class _Slot:
    """Um template da definição: o texto, se é HTML e as variáveis do contexto dele."""

    source: str
    html: bool
    context: frozenset[str]
    checks: Mapping[str, Callable[[str], str | None]]


def _problem(error: TemplateProblem) -> Problem:
    return Problem(error.name, error.detail, error.line)


def compile_templates(
    definition: FormDocument,
    *,
    plan_model: type[BaseModel],
    project_plan: Callable[[Mapping[str, Any], str], dict[str, Any]],
    example_answers: BaseModel,
    example_plan: BaseModel,
    lead_name: str | None,
    clock: Callable[[], datetime],
    scoring: Scoring | None = None,
    calendar: Calendar | None = None,
) -> tuple[FormTemplates, str]:
    """Os templates compilados e as instruções já renderizadas. A pontuação (se houver) calcula as
    notas das respostas de exemplo, que o prompt, o PDF e a prévia recebem na renderização do
    exemplo. Levanta `DefinitionError` com os problemas de todos os templates."""
    released = scoring.released(definition.pdf.scores) if scoring is not None else frozenset()
    checks = _field_checks(definition, scoring)
    page_checks = _field_checks(definition, scoring, released)
    slots = {
        "instructions": _Slot(definition.instructions, False, INSTRUCTIONS_CONTEXT, checks),
        "prompt": _Slot(definition.prompt, False, PROMPT_CONTEXT, checks),
        "pdf.html": _Slot(definition.pdf.html, True, PAGE_CONTEXT, page_checks),
        "preview": _Slot(definition.preview, True, PAGE_CONTEXT, page_checks),
    }
    data = template_data(definition.data)
    problems: list[Problem] = []
    compiled: dict[str, SandboxTemplate] = {}
    clean: set[str] = set()  # compilaram e passaram na conferência: podem renderizar o exemplo
    for path, slot in slots.items():
        try:
            compiled[path] = SandboxTemplate(slot.source, name=path, html=slot.html)
        except TemplateProblem as exc:
            problems.append(_problem(exc))
            continue
        found = _references(compiled[path], slot.context, slot.checks)
        if "phases" in slot.context and calendar is None:
            line = compiled[path].undeclared().get("phases")
            if line is not None:
                found.append(Problem(path, "o formulário não tem fases com datas (phases)", line))
        problems += found
        if not found:
            clean.add(path)
    if len(compiled) < len(slots):
        raise DefinitionError(problems)

    templates = FormTemplates(
        prompt=compiled["prompt"],
        pdf=compiled["pdf.html"],
        pdf_css=definition.pdf.css,
        preview=compiled["preview"],
        plan_model=plan_model,
        project_plan=project_plan,
        labels=labels_of(definition.steps),
        data=data,
        clock=clock,
        scoring=scoring,
        reads_scores=compiled["prompt"].reads("scores") != (),
        released=released,
        calendar=calendar,
    )
    example_scores = scoring.run(example_answers).result.scores if scoring is not None else {}
    page = {
        "lead": {"name": lead_name},
        "form": {"slug": definition.slug, "title": definition.title},
        "scores": example_scores,
        "generated_date": long_date(clock()),
    }
    examples: dict[str, Callable[[], str]] = {
        "instructions": lambda: compiled["instructions"].render({"data": data}),
        "prompt": lambda: templates.render_prompt(example_answers),
        "pdf.html": lambda: templates.render_pdf(example_plan, **page),
        "preview": lambda: templates.render_preview(example_plan.model_dump(mode="json"), **page),
    }
    rendered: dict[str, str] = {}
    for path, render in examples.items():
        if path not in clean:
            continue
        try:
            rendered[path] = render()
        except TemplateProblem as exc:
            problems.append(_problem(exc))
    if problems:
        raise DefinitionError(problems)
    return templates, rendered["instructions"]
