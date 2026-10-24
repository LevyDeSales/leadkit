"""As páginas do rascunho no admin, já no formato das telas: o editor, o teste, a publicação e a
comparação de versões de um formulário do banco (`app.forms.drafts` e `app.forms.draft_tests`).
Datas no horário de Brasília e textos em pt-BR; os templates só dispõem o que recebem. Toda página
traz o formulário e os endereços das outras (`DraftNav`), montados num lugar só."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.admin.asset_pages import TYPE_LABELS, asset_url
from app.admin.condition_editor import (
    ConditionsView,
    conditions_view,
    where_words,
)
from app.admin.condition_editor import place_of as condition_place
from app.admin.data_editor import DataView
from app.admin.form_editor import (
    KINDS,
    NOTICE_PAGES,
    FormEdit,
    QuestionEdit,
    add_option_id,
    field_id,
)
from app.admin.plan_editor import PlanView, plan_view
from app.admin.reading import QUESTION_KINDS, shown_number
from app.admin.scoring_editor import ScoringView, scoring_view
from app.admin.views import (
    DRAFT_REF,
    FAILURE_LABELS,
    Badge,
    ContactInPromptView,
    DraftNav,
    DraftSummary,
    Moment,
    contact_roles,
    draft_nav,
    draft_summary,
    moment,
)
from app.forms import IDENTITY_ROLES, FormSpec
from app.forms import assets as form_assets
from app.forms.dates import long_date
from app.forms.definition import DefinitionError, Problem, compile_definition, utc_now
from app.forms.definition.changes import (
    DroppedValue,
    SectionChange,
    dropped_option_values,
    reads,
)
from app.forms.definition.components import COMPONENTS, Component
from app.forms.definition.document import parse
from app.forms.definition.questions import CHOICE_KINDS, IDENTITY_KINDS, questions_of
from app.forms.definition.resources import asset_names
from app.forms.draft_previews import EXAMPLE, TEST, last_test
from app.forms.draft_tests import (
    GATE_CURRENT,
    GATE_STALE,
    GATE_UNLOCKS,
    INTERRUPTED_CODE,
    KEEP_TESTS,
    NOT_STARTED_CODE,
    TEST_DAYS,
    DraftTestResult,
    gate_of,
)
from app.forms.drafts import (
    TEST_MISSING,
    TEST_NOT_NEEDED,
    Assembled,
    Draft,
    DraftTexts,
    Review,
    Scored,
    TemplateTexts,
    changed,
    score,
    template_texts_of,
    texts_of,
)
from app.forms.stored import NOTE_MAX_LENGTH

# --- variáveis do prompt ----------------------------------------------------------------------


# O que cada tipo de bloco dos dados dá ao template.
DATA_NOTES = {
    "value": "dados do formulário, como estão escritos",
    "list": "lista de nomes",
    "windows": "janelas pelo nome, cada uma com name, label, start e end",
    "edition": "edição, com name, starts e ends",
}


@dataclass(frozen=True)
class Variable:
    name: str  # como o template a escreve: `answers.nome`
    note: str


@dataclass(frozen=True)
class Variables:
    """O que as instruções e o prompt podem usar e as respostas que o prompt cita."""

    prompt: tuple[Variable, ...]
    instructions: tuple[Variable, ...]
    cited: tuple[str, ...] | None  # as perguntas que o prompt cita; None: todas (usa `answers`)


def variables(draft: Draft) -> Variables | None:
    """As variáveis do rascunho; None se a definição não lê (uma versão que não compila mais)."""
    try:
        definition = parse(draft.document)
    except DefinitionError:
        return None
    data = tuple(
        Variable(f"data.{name}", DATA_NOTES[block.kind]) for name, block in definition.data.items()
    )
    prompt: list[Variable] = []
    for question in questions_of(definition.steps):
        kind = QUESTION_KINDS.get(question.kind, question.kind)
        prompt.append(Variable(f"answers.{question.name}", f"{kind} · {question.label}"))
        if question.kind in CHOICE_KINDS:
            prompt.append(
                Variable(
                    f"labels.{question.name}", f"o rótulo da opção escolhida · {question.label}"
                )
            )
    if definition.scoring is not None:
        for score_doc in definition.scoring.scores:
            prompt.append(Variable(f"scores.{score_doc.name}", "nota da pontuação"))
            if score_doc.bands is not None:
                prompt.append(Variable(f"scores.{score_doc.bands.key}", "nível da faixa da nota"))
                prompt.append(
                    Variable(f"scores.{score_doc.bands.label}", "rótulo da faixa da nota")
                )
    prompt.append(Variable("today", "a data de hoje em São Paulo; no teste, a data simulada"))
    if definition.phases is not None:
        prompt.append(
            Variable(
                "phases",
                "uma entrada por fase: number, first, last, period, situation, windows e allowed",
            )
        )
    return Variables(
        prompt=(*prompt, *data), instructions=data, cited=reads(draft.document).questions
    )


# --- a pontuação ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredView:
    """A pontuação com certas respostas, como o editor e o teste a mostram: cada nota, a prioridade
    e a persona, com a regra (a partir de 1) que decidiu cada uma."""

    examples: bool  # as respostas de exemplo
    declared: bool  # o rascunho tem pontuação
    rows: tuple[tuple[str, str], ...]  # (nome, valor), na ordem em que a pontuação os declara
    priority: str | None
    priority_rule: int | None
    has_priority_rules: bool
    persona: str | None
    persona_rule: int | None
    has_persona_rules: bool


def scored_view(scored: Scored) -> ScoredView:
    scoring, run = scored.spec.scoring, scored.run
    if scoring is None or run is None:
        return ScoredView(scored.examples, False, (), None, None, False, None, None, False)
    return ScoredView(
        examples=scored.examples,
        declared=True,
        rows=tuple((name, shown_number(value)) for name, value in run.result.scores.items()),
        priority=run.result.priority,
        priority_rule=None if run.priority_rule is None else run.priority_rule + 1,
        has_priority_rules=scoring.has_priority,
        persona=run.result.persona,
        persona_rule=None if run.persona_rule is None else run.persona_rule + 1,
        has_persona_rules=scoring.has_persona,
    )


def example_scores(draft: Draft) -> ScoredView | None:
    """A pontuação do rascunho guardado com as respostas de exemplo, para o editor; None se ele
    não tem pontuação ou não calcula (o editor já mostra os problemas)."""
    try:
        scored = score(draft, now=utc_now())
    except DefinitionError:
        return None
    return scored_view(scored) if scored.run is not None else None


# --- o editor ---------------------------------------------------------------------------------


# As partes da definição que o editor estruturado cobre: o problema nelas volta para o campo.
STRUCTURED = (
    "title",
    "intro",
    "minutes",
    "slug",
    "steps",
    "identity",
    "consent",
    "plan",
    "phases",
    "data",
    "scoring",
    "notices",
    "completion",
)
TEXT_FIELDS = ("instructions", "prompt", "definition")  # os campos de texto do editor
FIELD_NAMES = {
    "title": "título",
    "help": "ajuda",
    "name": "nome da resposta",
    "kind": "tipo",
    "label": "rótulo",
    "placeholder": "texto no campo vazio",
    "autocomplete": "preenchimento automático",
    "required": "obrigatória",
    "contact": "dado de contato",
    "min_length": "tamanho mínimo",
    "max_length": "tamanho máximo",
    "min_choices": "mínimo de escolhas",
    "max_choices": "máximo de escolhas",
    "options": "opções",
    "show_when": "condição",
    "value": "valor",
    "points": "pontos",
    "example": "resposta de exemplo",
}
COMPLETION_NAMES = {
    "mode": "modo",
    "message": "mensagem de confirmação",
    "button_label": "rótulo do botão",
    "url": "destino HTTPS",
    "handoff.selection": "referência de encaminhamento",
}
PLACE_NAMES_COMPLETION = {
    f"completion.{key}": f"Conclusão · {value}" for key, value in COMPLETION_NAMES.items()
}
PLACE_NAMES = {
    "release.mode": "Liberação do plano",
    "title": "Título",
    "intro": "Introdução",
    "minutes": "Tempo para responder",
    "slug": "Endereço",
    "steps": "Etapas",
    "identity": "Identidade da lead",
    "identity.email": "Identidade da lead · e-mail",
    "identity.name": "Identidade da lead · nome",
    "identity.phone": "Identidade da lead · telefone",
    "consent.processing": "Consentimento · autorização do processamento",
    "consent.acquisition_method": "Consentimento · como a participante concorda",
    "consent.presentation": "Consentimento · apresentação do aviso",
    "consent.position": "Consentimento · posição do aviso",
    "consent.notice": "Consentimento · aviso sobre o uso dos dados",
    "consent.marketing": "Consentimento · canais de novidades",
    "consent.marketing.email": "Consentimento · novidades por e-mail",
    "consent.marketing.whatsapp": "Consentimento · novidades por WhatsApp",
}
# Os avisos das páginas de espera e de falha: cada página com o título do bloco e quando a lead a vê;
# e cada campo do aviso, com o nome que o problema leva.
NOTICE_PAGE_TEXTS = {
    "waiting": (
        "Aviso na página de espera",
        "Aparece enquanto o plano é montado. Em branco, a página sai sem aviso.",
    ),
    "failed": (
        "Aviso na página de falha",
        (
            "Aparece se o plano não puder ser montado, ao lado do código de atendimento. "
            "Em branco, a página sai sem aviso."
        ),
    ),
}
NOTICES_SHOWN = tuple((page, *NOTICE_PAGE_TEXTS[page]) for page in NOTICE_PAGES)
NOTICE_FIELD_NAMES = {
    "title": "título",
    "text": "texto",
    "link.label": "rótulo do link",
    "link.url": "endereço do link",
}
PLACE_NAMES |= {f"notices.{page}": shown for page, shown, _ in NOTICES_SHOWN} | {
    f"notices.{page}.{field}": f"{shown} · {name}"
    for page, shown, _ in NOTICES_SHOWN
    for field, name in NOTICE_FIELD_NAMES.items()
}
# Os templates e as notas do PDF: o campo do editor do PDF onde cada um mora e como o problema o
# chama. Um problema do `pdf` inteiro (o PDF de exemplo que não saiu) vai para o HTML.
TEMPLATE_PLACES = {
    "pdf.html": ("pdf_html", "PDF (HTML)"),
    "pdf.css": ("pdf_css", "PDF (CSS)"),
    "preview": ("preview", "Prévia da página de resultado"),
    "pdf.scores": ("pdf_scores", "Notas do PDF"),
    "pdf": ("pdf_html", "PDF"),
}
IDENTITY_LABELS = {"email": "E-mail da lead", "name": "Nome da lead", "phone": "Telefone da lead"}
IDENTITY_HINTS = {
    "email": "Obrigatório: uma pergunta de e-mail obrigatória.",
    "name": "Opcional: uma pergunta de texto, que fica fora do rascunho no aparelho.",
    "phone": "Opcional: uma pergunta de telefone. As novidades por WhatsApp precisam dele.",
}
# Cada canal de novidades: a caixa que o oferece e o rótulo do texto dele.
CHANNELS_SHOWN = (
    ("email", "Oferecer as novidades por e-mail", "Texto das novidades por e-mail"),
    ("whatsapp", "Oferecer as novidades por WhatsApp", "Texto das novidades por WhatsApp"),
)


def template_place(path: str) -> str | None:
    """A chave de `TEMPLATE_PLACES` do caminho de um problema do PDF, da prévia ou das notas do
    PDF; None se o caminho é de outra parte da definição."""
    if path in TEMPLATE_PLACES:
        return path
    return "pdf.scores" if path.startswith("pdf.scores.") else None


def _notice_place(path: str) -> str:
    """O campo do aviso onde o problema aparece: o próprio campo (`notices.waiting.link.url`) ou, se
    o caminho aponta para o aviso inteiro, a caixa da página dele."""
    if path in PLACE_NAMES:
        return path
    parts = path.split(".")
    return (
        f"notices.{parts[1]}" if len(parts) > 1 and parts[1] in NOTICE_PAGES else "notices.waiting"
    )


def place_of(problem: Problem, edit: FormEdit, *, slug_field: bool = False) -> str:
    """O campo do editor onde o problema aparece, pelo caminho na definição: um campo do editor
    estruturado (o nome dele), uma caixa dele (a etapa, a pergunta, as opções), o campo das
    instruções, o do prompt ou o JSON do resto (`definition`). O endereço só tem campo antes da
    primeira publicação (`slug_field`); depois, o problema nele vem do JSON."""
    path = problem.path
    if path == "generation":
        return "generation"
    if path.startswith("release"):
        return "release.mode"
    if path.startswith("completion.handoff"):
        return "completion.handoff.selection"
    if path.startswith("completion"):
        return (
            path
            if path
            in {
                "completion.mode",
                "completion.message",
                "completion.button_label",
                "completion.url",
            }
            else "completion.mode"
        )
    if path == "slug" and not slug_field:
        return "definition"
    if template_place(path) is not None:
        return "pdf-editor"  # o editor do PDF, noutra página
    if path.startswith("instructions"):
        return "instructions"
    if path.startswith("prompt"):
        return "prompt"
    parts = path.split(".")
    if path.startswith("examples.answers"):
        place = edit.path_of(parts[2]) if len(parts) > 2 else None
        return f"{place}.example" if place else "steps"
    if parts[0] in ("plan", "phases"):
        return edit.plan.place_of(path)
    if parts[0] == "data":
        return edit.data.place_of(path)
    if parts[0] == "scoring":
        return edit.scoring.place_of(path)
    if parts[0] == "notices":
        return _notice_place(path)
    if parts[0] not in STRUCTURED:
        return "definition"
    if parts[0] == "steps" and len(parts) >= 5 and parts[2] == "questions":
        question = ".".join(parts[:4])
        numbered = parts[1].isdigit() and parts[3].isdigit()
        found = edit.question_at(int(parts[1]), int(parts[3])) if numbered else None
        if found is not None and parts[4] == "show_when":
            return condition_place(parts, 4)
        if found is None or parts[4] not in found.fields:
            return question
        if parts[4] == "options":
            if len(parts) > 6 and parts[6] == "show_when" and parts[5].isdigit():
                return condition_place(parts, 6)
            return path
        return ".".join(parts[:7])
    if parts[0] == "steps" and len(parts) == 3:  # `steps.0.questions`: a etapa sem perguntas
        return ".".join(parts[:2])
    return path


def where(problem: Problem, edit: FormEdit, *, slug_field: bool = False) -> str:
    """Onde está o problema, como a operadora lê: "Etapa 1 · pergunta 2 · rótulo"."""
    path = problem.path
    if path in PLACE_NAMES_COMPLETION:
        return PLACE_NAMES_COMPLETION[path]
    parts = path.split(".")
    if path == "slug" and not slug_field:
        return path
    if path.startswith("examples.answers.") and len(parts) > 2:
        place = edit.path_of(parts[2])
        if place is not None:
            return where(Problem(f"{place}.example", problem.message), edit)
    if parts[0] == "steps" and len(parts) > 1 and parts[1].isdigit():
        found = [f"Etapa {int(parts[1]) + 1}"]
        rest = parts[2:]
        if rest[:1] == ["questions"] and len(rest) > 1 and rest[1].isdigit():
            found.append(f"pergunta {int(rest[1]) + 1}")
            rest = rest[2:]
        if rest[:1] == ["options"] and len(rest) > 1 and rest[1].isdigit():
            found.append(f"opção {int(rest[1]) + 1}")
            rest = rest[2:]
        if rest[:1] == ["show_when"]:
            found += [FIELD_NAMES["show_when"], *where_words(rest[1:])]
        elif rest:
            found.append(FIELD_NAMES.get(rest[0], rest[0]))
        return " · ".join(found)
    if parts[0] in ("plan", "phases"):
        return edit.plan.describe(edit.plan.place_of(path))
    if parts[0] == "data":
        return edit.data.describe(edit.data.place_of(path))
    if parts[0] == "scoring":
        return edit.scoring.describe(edit.scoring.place_of(path))
    if path in PLACE_NAMES:
        return PLACE_NAMES[path]
    template = template_place(path)
    if template is not None:
        shown = TEMPLATE_PLACES[template][1]
        return f"{shown}, linha {problem.line}" if problem.line is not None else shown
    shown = path or "definição"
    return f"{shown}, linha {problem.line}" if problem.line is not None else shown


@dataclass(frozen=True)
class ShownProblem:
    """Um problema na lista do topo do editor: onde, o que e o campo para onde o link leva."""

    where: str
    message: str
    target: str  # o id do campo ou da caixa
    href: str = ""  # o endereço do campo, quando ele está em outra página (o editor do PDF)
    excerpt: str = ""  # a linha do template onde está o problema, como a operadora a escreveu
    line: int | None = None  # a linha do template, para o link levar até ela


@dataclass(frozen=True)
class IdentityChoice:
    """Um papel da identidade da lead e as perguntas que podem tê-lo."""

    role: str
    label: str
    hint: str
    options: list[tuple[str, str]]  # (a posição `etapa.pergunta`, o texto da opção)
    selected: str  # a posição da pergunta escolhida; vazio: nenhuma


def _identity(edit: FormEdit) -> list[IdentityChoice]:
    places: list[tuple[QuestionEdit, str, str]] = [
        (question, f"{i}.{j}", f"Etapa {i + 1} · {question.label.strip() or 'pergunta nova'}")
        for i, step in enumerate(edit.steps)
        for j, question in enumerate(step.questions)
    ]
    found = []
    for role in IDENTITY_ROLES:
        chosen = edit.identity.get(role)
        found.append(
            IdentityChoice(
                role=role,
                label=IDENTITY_LABELS[role],
                hint=IDENTITY_HINTS[role],
                options=[
                    (place, text)
                    for question, place, text in places
                    if question.kind == IDENTITY_KINDS[role] or question is chosen
                ],
                selected=next((place for q, place, _ in places if q is chosen), ""),
            )
        )
    return found


@dataclass(frozen=True)
class StructureView:
    """O editor estruturado, como a página o desenha: o editor, o problema de cada campo, o campo
    que recebe o foco, as perguntas abertas e o que a identidade pode escolher."""

    edit: FormEdit
    errors: Mapping[str, str]  # pelo nome do campo ou da caixa
    focus: str | None  # o id do campo que recebe o foco
    troubled: frozenset[str]  # as perguntas com problema (`etapa.pergunta`)
    opened: frozenset[str]  # as perguntas abertas: as com problema e a do foco
    kinds: list[tuple[str, str]]
    identity: list[IdentityChoice]
    channels: tuple[tuple[str, str, str], ...]  # (o canal, a caixa, o rótulo do texto)
    notices: tuple[tuple[str, str, str], ...]  # (a página, o título do aviso, quando a lead o vê)
    dropped: tuple[DroppedValue, ...]  # os valores publicados que o rascunho tirou
    edit_url: str  # para onde vão as operações (incluir, remover, reordenar)
    slug: str | None  # o endereço que ainda dá para mudar (antes da primeira publicação)
    plan: PlanView  # o modelo do plano (`app.admin.plan_editor`)
    data: DataView  # os dados do formulário (`app.admin.data_editor`)
    scoring: ScoringView  # a pontuação (`app.admin.scoring_editor`)
    conditions: (
        ConditionsView  # as condições das perguntas e das opções (`app.admin.condition_editor`)
    )

    def kind_label(self, kind: str) -> str:
        return QUESTION_KINDS.get(kind, kind)

    def field_id(self, name: str) -> str:
        """O id do campo na página, pelo nome dele (`form_editor.field_id`)."""
        return field_id(name)


def structure_view(
    draft: Draft,
    edit: FormEdit,
    *,
    problems: Sequence[Problem] = (),
    focus: str | None = None,
    slug: str | None = None,
) -> StructureView:
    errors: dict[str, str] = {}
    for problem in problems:
        errors.setdefault(place_of(problem, edit, slug_field=not draft.published), problem.message)
    troubled = {
        f"{parts[1]}.{parts[3]}"
        for parts in (name.split(".") for name in errors)
        if len(parts) >= 4 and parts[0] == "steps" and parts[2] == "questions"
    }
    opened = set(troubled)
    for i, step in enumerate(edit.steps):
        for j in range(len(step.questions)):
            # O foco está num campo da pergunta, ou no botão de adicionar opção dela (o foco de
            # quem remove uma opção), que não leva o prefixo dos campos.
            inside = field_id(f"steps.{i}.questions.{j}.")
            if focus is not None and (focus.startswith(inside) or focus == add_option_id(i, j)):
                opened.add(f"{i}.{j}")
    published = draft.current.definition if draft.current is not None else None
    dropped: tuple[DroppedValue, ...] = ()
    if published is not None:
        dropped = dropped_option_values(published, {"steps": edit.screen()["steps"]})
    return StructureView(
        edit=edit,
        errors=errors,
        focus=focus,
        troubled=frozenset(troubled),
        opened=frozenset(opened),
        kinds=[(kind, QUESTION_KINDS[kind]) for kind in KINDS],
        identity=_identity(edit),
        channels=CHANNELS_SHOWN,
        notices=NOTICES_SHOWN,
        dropped=dropped,
        edit_url=f"{draft_nav(draft).draft_url}/edit",
        slug=None if draft.published else (draft.slug if slug is None else slug),
        plan=plan_view(edit.plan, edit.data.list_names(), errors, focus, edit.data.window_blocks()),
        data=DataView(edit.data),
        scoring=scoring_view(edit.scoring, edit.choice_names(), errors, focus),
        conditions=conditions_view(edit),
    )


def _template_href(problem: Problem, draft: Draft) -> str:
    """O endereço do campo do editor do PDF onde está o problema de um template do PDF ou da
    prévia; vazio para o problema de qualquer outra parte."""
    key = template_place(problem.path)
    if key is None:
        return ""
    return f"{draft_nav(draft).pdf_url}#{field_id(TEMPLATE_PLACES[key][0])}"


@dataclass(frozen=True)
class EditorPage:
    """O editor do rascunho, com o que a operadora mandou e os problemas, se o salvar falhou."""

    nav: DraftNav
    summary: DraftSummary
    texts: DraftTexts
    token: str
    problems: Sequence[ShownProblem]
    invalid: frozenset[str]  # os campos de texto com problema
    message: str | None  # por que nada foi salvo, quando não é um problema da definição
    variables: Variables | None
    structure: StructureView
    unsaved: bool  # a página traz uma operação do editor que ainda não foi salva
    generation_fields: bool  # conserva a configuração enquanto a retirada não foi salva
    saved_choices: tuple[tuple[str, str], ...]
    scoring: ScoredView | None  # a pontuação do rascunho guardado com as respostas de exemplo


def editor(
    draft: Draft,
    *,
    texts: DraftTexts | None = None,
    edit: FormEdit | None = None,
    problems: Sequence[Problem] = (),
    message: str | None = None,
    focus: str | None = None,
    token: str | None = None,
    slug: str | None = None,
    unsaved: bool = False,
) -> EditorPage:
    """O editor do rascunho guardado ou, com `texts` e `edit`, o que a operadora mandou. `token`
    é a impressão digital que a página mostrou, mantida numa operação do editor: quem salva depois
    ainda esbarra numa mudança feita em outra aba."""
    edit = edit or FormEdit.of_document(draft.document)
    slug_field = not draft.published
    places = [place_of(problem, edit, slug_field=slug_field) for problem in problems]
    return EditorPage(
        nav=draft_nav(draft),
        summary=draft_summary(draft),
        texts=texts or texts_of(draft.document),
        token=token or draft.token,
        problems=[
            ShownProblem(
                where(problem, edit, slug_field=slug_field),
                problem.message,
                field_id(place),
                href=_template_href(problem, draft),
            )
            for problem, place in zip(problems, places, strict=True)
        ],
        invalid=frozenset(place for place in places if place in TEXT_FIELDS),
        message=message,
        variables=variables(draft),
        structure=structure_view(
            draft,
            edit,
            problems=problems,
            focus=focus or (field_id(places[0]) if places else None),
            slug=slug,
        ),
        unsaved=unsaved,
        generation_fields=edit.generation == "pdf"
        or draft.document.get("generation", "pdf") == "pdf",
        saved_choices=(
            (
                "Geração",
                "Somente salvar respostas"
                if draft.document.get("generation") == "none"
                else "Gerar plano e PDF",
            ),
            (
                "Conclusão",
                {
                    "result": "Acompanhar o resultado do PDF",
                    "confirmation": "Confirmação local",
                    "button": "Botão para outro destino",
                    "redirect": "Encaminhamento para outro destino",
                }.get(draft.document.get("completion", {}).get("mode", "result"), "Indisponível"),
            ),
            (
                "Liberação",
                "Não se aplica"
                if draft.document.get("generation") == "none"
                else (
                    "Manual"
                    if draft.document.get("release", {}).get("mode") == "manual"
                    else "Automática"
                ),
            ),
        ),
        scoring=example_scores(draft),
    )


# --- o PDF e a prévia ------------------------------------------------------------------------

PDF_FIELDS = ("pdf_html", "pdf_css", "preview", "pdf_scores")  # os campos do editor do PDF


@dataclass(frozen=True)
class NoteChoice:
    """Uma nota da pontuação e se o PDF e a prévia podem mostrá-la."""

    name: str
    levels: str  # os nomes do nível e do rótulo da faixa, que vêm com a nota; vazio sem faixa
    checked: bool


def _score_docs(document: Mapping[str, Any]) -> list[Any]:
    """As notas da pontuação do documento; vazio sem pontuação ou se a definição não lê."""
    try:
        scoring = parse(document).scoring
    except DefinitionError:
        return []
    return list(scoring.scores) if scoring is not None else []


def _notes(document: Mapping[str, Any], chosen: Sequence[str]) -> tuple[NoteChoice, ...]:
    return tuple(
        NoteChoice(
            name=score_doc.name,
            levels=f"{score_doc.bands.key}, {score_doc.bands.label}" if score_doc.bands else "",
            checked=score_doc.name in chosen,
        )
        for score_doc in _score_docs(document)
    )


def _page_variables(document: Mapping[str, Any], chosen: Sequence[str]) -> tuple[Variable, ...]:
    """O que o PDF e a prévia podem usar: o plano sem os campos internos, a lead, o formulário, as
    notas liberadas, a data e a biblioteca da marca."""
    found = [Variable("plan.<campo>", "o plano; os campos que não são internos")]
    try:
        definition = parse(document)
    except DefinitionError:
        definition = None
    if definition is not None:
        found = [
            Variable(f"plan.{field.name}", f"{field.kind} do plano")
            for field in definition.plan.fields
            if not field.internal
        ]
    found += [
        Variable("lead.name", "o nome da lead, se o formulário o pede"),
        Variable("form.title", "o título do formulário"),
        Variable("form.slug", "o endereço do formulário"),
    ]
    for score_doc in _score_docs(document):
        if score_doc.name in chosen:
            found.append(Variable(f"scores.{score_doc.name}", "nota liberada para o PDF"))
            if score_doc.bands is not None:
                found.append(Variable(f"scores.{score_doc.bands.key}", "nível da faixa da nota"))
                found.append(Variable(f"scores.{score_doc.bands.label}", "rótulo da faixa da nota"))
    found += [
        Variable("generated_date", "a data da geração, por extenso, em São Paulo"),
        Variable("brand", "os componentes da marca, listados ao lado"),
    ]
    return tuple(found)


@dataclass(frozen=True)
class ImageChoice:
    """Uma imagem que os templates deste formulário enxergam, para a operadora pedi-la pelo nome."""

    name: str
    reference: str  # `asset:<nome>`, como o template a pede
    thumbnail: str
    scope: str
    detail: str  # o que ela é: tipo e dimensões
    url: str  # a página dela no admin
    used: bool  # algum dos três templates, como estão na página, a pede


def _image_choices(
    images: Sequence[form_assets.Asset], texts: TemplateTexts
) -> tuple[ImageChoice, ...]:
    used = set(asset_names(texts.pdf_html, texts.pdf_css, texts.preview))
    return tuple(
        ImageChoice(
            name=asset.name,
            reference=f"asset:{asset.name}",
            thumbnail=asset.ref.url,
            scope=asset.scope,
            detail=f"{TYPE_LABELS[asset.ref.content_type]}, {asset.width} × {asset.height}",
            url=asset_url(asset.id),
            used=asset.name in used,
        )
        for asset in images
    )


@dataclass(frozen=True)
class PdfPage:
    """O editor do PDF e da prévia da página de resultado: os três templates, as notas que o PDF
    pode mostrar, as prévias, a biblioteca de componentes e as imagens que o formulário enxerga.
    Com o que a operadora mandou e os problemas, se o salvar falhou."""

    nav: DraftNav
    summary: DraftSummary
    texts: TemplateTexts
    token: str
    problems: Sequence[ShownProblem]
    invalid: frozenset[str]  # os campos com problema
    message: str | None  # por que nada foi salvo, quando não é um problema da definição
    notes: tuple[NoteChoice, ...]
    last_test: Moment | None  # quando terminou o último teste com sucesso; None: não há
    source: str  # o plano das prévias: EXAMPLE ou TEST
    components: tuple[Component, ...]
    variables: tuple[Variable, ...]
    images: tuple[ImageChoice, ...] = ()

    @property
    def preview_url(self) -> str:
        return f"{self.nav.pdf_url}/preview"

    @property
    def result_url(self) -> str:
        return f"{self.nav.pdf_url}/result"

    def field_id(self, name: str) -> str:
        return field_id(name)


def _excerpt(problem: Problem, texts: TemplateTexts) -> str:
    """A linha do template onde está o problema, como a operadora a escreveu."""
    source = {"pdf.html": texts.pdf_html, "pdf.css": texts.pdf_css, "preview": texts.preview}.get(
        problem.path
    )
    if source is None or problem.line is None:
        return ""
    lines = source.replace("\r\n", "\n").split("\n")
    return lines[problem.line - 1].strip()[:160] if 1 <= problem.line <= len(lines) else ""


def pdf_editor(
    draft: Draft,
    *,
    texts: TemplateTexts | None = None,
    problems: Sequence[Problem] = (),
    message: str | None = None,
    token: str | None = None,
    tests: Sequence[DraftTestResult] = (),
    source: str = EXAMPLE,
    images: Sequence[form_assets.Asset] = (),
) -> PdfPage:
    """O editor do PDF do rascunho guardado ou, com `texts`, o que a operadora mandou. `images` são
    as imagens que o formulário enxerga (`app.forms.assets.visible`)."""
    texts = texts or template_texts_of(draft.document)
    shown = []
    for problem in problems:
        key = template_place(problem.path)
        target, _ = TEMPLATE_PLACES[key] if key is not None else ("", "")
        shown.append(
            ShownProblem(
                where(problem, FormEdit(), slug_field=False),
                problem.message,
                field_id(target) if target else "problemas",
                excerpt=_excerpt(problem, texts),
                line=problem.line,
            )
        )
    places = {TEMPLATE_PLACES[key][0] for p in problems if (key := template_place(p.path))}
    test = last_test(tests)
    return PdfPage(
        nav=draft_nav(draft),
        summary=draft_summary(draft),
        texts=texts,
        token=token or draft.token,
        problems=shown,
        invalid=frozenset(places),
        message=message,
        notes=_notes(draft.document, texts.scores),
        last_test=moment(test.finished_at) if test is not None else None,
        source=source if source in (EXAMPLE, TEST) else EXAMPLE,
        components=COMPONENTS,
        variables=_page_variables(draft.document, texts.scores),
        images=_image_choices(images, texts),
    )


# --- a prévia da página pública ---------------------------------------------------------------

PHONE_WIDTH = 375  # a largura da prévia, em pixels: a de um celular comum
PHONE_HEIGHT = 740


@dataclass(frozen=True)
class PreviewStep:
    number: int
    title: str
    url: str  # o quadro da prévia nesta etapa


@dataclass(frozen=True)
class PreviewPage:
    """A prévia da página pública do rascunho guardado: um quadro na largura de um celular, que
    mostra uma etapa por vez, e o atalho para cada etapa."""

    nav: DraftNav
    summary: DraftSummary
    steps: list[PreviewStep]
    frame_url: str  # a primeira etapa
    width: int
    height: int


def _spec(draft: Draft) -> FormSpec:
    """O contrato do rascunho guardado. Levanta `DefinitionError` se ele não compila mais (um
    deploy com o compilador mais rígido)."""
    return compile_definition(draft.document, version_number=draft.next_number)


def preview_page(draft: Draft) -> PreviewPage:
    nav = draft_nav(draft)
    screen = _spec(draft).screen
    titles = [step.title for step in screen.steps] if screen is not None else []
    steps = [
        PreviewStep(number, title, f"{nav.preview_url}/frame?etapa={number}")
        for number, title in enumerate(titles, start=1)
    ]
    return PreviewPage(
        nav=nav,
        summary=draft_summary(draft),
        steps=steps,
        frame_url=steps[0].url if steps else f"{nav.preview_url}/frame",
        width=PHONE_WIDTH,
        height=PHONE_HEIGHT,
    )


def step_of(asked: str) -> int:
    """A etapa em que a prévia abre, do `?etapa=` da página: um número de até 4 algarismos, a partir
    de 1. Qualquer outra coisa abre na primeira (a página não recusa a moldura por isso)."""
    return int(asked) if asked.isascii() and asked.isdigit() and len(asked) <= 4 else 1


# --- o teste ----------------------------------------------------------------------------------

TEST_BADGES = {
    "queued": Badge("Na fila", "clock", "neutral"),
    "running": Badge("Rodando", "loader", "prog"),
    "succeeded": Badge("Concluído", "check", "ok"),
    "failed": Badge("Falhou", "alert-circle", "err"),
    "interrupted": Badge("Interrompido", "alert-triangle", "warn"),
}
TEST_FAILURE_LABELS = {
    **FAILURE_LABELS,
    NOT_STARTED_CODE: "não começou",
    INTERRUPTED_CODE: "interrompido",
}
GATE_NOTES = {
    GATE_CURRENT: "Este teste libera a publicação: ele testou o rascunho como está agora.",
    GATE_UNLOCKS: (
        "Este teste libera a publicação: depois dele, o rascunho só mudou em partes que não vão "
        "ao LLM."
    ),
    GATE_STALE: "O que vai ao LLM mudou depois deste teste: ele não vale para a publicação.",
}
PROGRESS = {
    "queued": "Esperando o worker pegar o teste.",
    "running": "O LLM está escrevendo o plano; depois vem o PDF.",
}
POLL_SECONDS = 2  # o intervalo da página do teste enquanto ele roda (HTMX)
REFRESH_SECONDS = 5  # o mesmo, sem JavaScript


def _number(value: int) -> str:
    """O número com o separador de milhar em português (3.120)."""
    return f"{value:,}".replace(",", ".")


def _duration(ms: int | None) -> str | None:
    if ms is None:
        return None
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s".replace(".", ",")


def _tokens(usage: Mapping[str, Any]) -> str | None:
    if usage.get("fake"):
        return "nenhum: o LLM falso do modo de teste respondeu"
    parts = [
        f"{_number(usage[key])} {label}"
        for key, label in (
            ("input_tokens", "de entrada"),
            ("output_tokens", "de saída"),
            ("total_tokens", "no total"),
        )
        if isinstance(usage.get(key), int)
    ]
    return " · ".join(parts) or None


def _repairs(usage: Mapping[str, Any]) -> str | None:
    repairs = usage.get("repairs")
    if not isinstance(repairs, int):
        return None
    if repairs == 0:
        return "nenhuma rodada de reparo"
    return f"{repairs} {'rodada' if repairs == 1 else 'rodadas'} de reparo"


def _headline(result: DraftTestResult) -> str:
    if result.status == "failed" and result.failure_code:
        return f"Falhou: {TEST_FAILURE_LABELS.get(result.failure_code, result.failure_code)}"
    return TEST_BADGES[result.status].label


@dataclass(frozen=True)
class DraftTestRow:
    """Um teste na lista dos recentes."""

    url: str
    status: Badge
    headline: str
    started_by: str
    started: Moment | None
    simulated: str | None  # a data simulada, por extenso


def _rows(nav: DraftNav, results: Sequence[DraftTestResult]) -> list[DraftTestRow]:
    return [
        DraftTestRow(
            url=nav.result_url(result.id),
            status=TEST_BADGES[result.status],
            headline=_headline(result),
            started_by=result.started_by,
            started=moment(result.started_at),
            simulated=long_date(result.simulated_date) if result.simulated_date else None,
        )
        for result in results
    ]


@dataclass(frozen=True)
class DraftTestView:
    """O resultado de um teste, como a página dele mostra."""

    url: str
    status: Badge
    headline: str  # "Concluído", "Falhou: plano inválido"
    detail: str | None  # a mensagem do adaptador, sem conteúdo
    finished: bool
    progress: str | None  # em que pé está, enquanto não termina
    started_by: str
    started: Moment | None
    simulated: str | None
    facts: list[tuple[str, str]]  # modelo, tempo, tokens, reparo, PDF
    repair_reasons: tuple[str, ...]
    plan: str | None  # o plano validado, em JSON
    pdf_url: str | None
    instructions: str
    prompt: str
    answers: str
    gate: str | None  # o que o teste diz à publicação, se terminou com sucesso


def _facts(result: DraftTestResult) -> list[tuple[str, str]]:
    usage = result.usage or {}
    timing = " · ".join(
        f"{label} em {value}"
        for label, value in (("LLM", _duration(result.llm_ms)), ("PDF", _duration(result.pdf_ms)))
        if value
    )
    pages = None
    if result.pdf_pages is not None:
        pages = f"PDF de {result.pdf_pages} {'página' if result.pdf_pages == 1 else 'páginas'}"
    facts = [
        (label, value)
        for label, value in (
            ("Modelo", result.model),
            ("Tempo", timing or None),
            ("Tokens", _tokens(usage)),
            ("Reparo", _repairs(usage)),
            ("PDF", pages),
        )
        if value
    ]
    if usage.get("fallback"):
        facts.append(("Reserva", "o modelo de reserva respondeu"))
    return facts


def _gate_note(draft: Draft, result: DraftTestResult) -> str | None:
    """O que o teste diz à publicação, em texto: a regra é do serviço (`draft_tests.gate_of`)."""
    gate = gate_of(draft, result)
    return GATE_NOTES[gate] if gate is not None else None


def result_view(draft: Draft, result: DraftTestResult) -> DraftTestView:
    nav = draft_nav(draft)
    reasons = (result.usage or {}).get("repair_reasons")
    return DraftTestView(
        url=nav.result_url(result.id),
        status=TEST_BADGES[result.status],
        headline=_headline(result),
        detail=result.failure_detail,
        finished=result.finished,
        progress=PROGRESS.get(result.status),
        started_by=result.started_by,
        started=moment(result.started_at),
        simulated=long_date(result.simulated_date) if result.simulated_date else None,
        facts=_facts(result),
        repair_reasons=tuple(str(reason) for reason in reasons)
        if isinstance(reasons, list)
        else (),
        plan=json.dumps(result.plan, ensure_ascii=False, indent=2)
        if result.plan is not None
        else None,
        pdf_url=f"{nav.result_url(result.id)}/pdf" if result.has_pdf else None,
        instructions=result.instructions,
        prompt=result.prompt,
        answers=json.dumps(result.answers, ensure_ascii=False, indent=2),
        gate=_gate_note(draft, result),
    )


@dataclass(frozen=True)
class DraftTestPage:
    """A página de um teste do rascunho, que se atualiza enquanto ele roda."""

    nav: DraftNav
    test: DraftTestView
    poll_seconds: int = POLL_SECONDS
    refresh_seconds: int = REFRESH_SECONDS


def result_page(draft: Draft, result: DraftTestResult) -> DraftTestPage:
    return DraftTestPage(nav=draft_nav(draft), test=result_view(draft, result))


@dataclass(frozen=True)
class CalendarRow:
    """Uma fase do calendário que o prompt recebe (`phases`), como a página do teste mostra."""

    number: int
    period: str
    windows: tuple[tuple[str, str], ...]  # (janela, situação nesta fase)
    allowed: tuple[tuple[str, str], ...]  # (campo, valores permitidos nesta fase)


def calendar_view(spec: FormSpec) -> tuple[CalendarRow, ...]:
    """O calendário das fases do formulário na data do relógio dele: vazio sem fases com datas."""
    phases = spec.templates.phases() if spec.templates is not None else []
    return tuple(
        CalendarRow(
            number=phase.number,
            period=phase.period,
            windows=tuple((window.label, window.text) for window in phase.windows.values()),
            allowed=tuple((name, ", ".join(values)) for name, values in phase.allowed.items()),
        )
        for phase in phases
    )


@dataclass(frozen=True)
class AssembledView:
    """O prompt montado, como a página do teste mostra."""

    instructions: str
    prompt: str
    today: str  # a data que o prompt recebeu, por extenso
    simulated: bool
    examples: bool
    prompt_version: str  # a que a publicação vai gravar nos planos
    calendar: tuple[CalendarRow, ...] = ()  # as fases com datas nessa data; vazio sem elas


@dataclass(frozen=True)
class DraftTestsPage:
    """O teste do rascunho: as respostas e a data, o prompt montado com elas e os testes
    recentes."""

    nav: DraftNav
    summary: DraftSummary
    answers: str
    day: str
    assembled: AssembledView | None
    scored: ScoredView | None
    problems: Sequence[Problem]
    message: str | None
    recent: list[DraftTestRow]
    keep: int  # quantos testes ficam guardados
    days: int  # por quantos dias


def shown_answers(given: str, assembled: Assembled | None, scored: Scored | None) -> str:
    """As respostas que o campo mostra: as que a montagem ou a pontuação usaram (as de exemplo,
    se vieram em branco) ou, se não deu para montar, as que a operadora mandou."""
    if assembled is not None:
        return assembled.answers_text
    return scored.answers_text if scored is not None else given


def testing_page(
    draft: Draft,
    *,
    answers: str,
    day: str,
    assembled: Assembled | None,
    scored: Scored | None = None,
    recent: Sequence[DraftTestResult],
    problems: Sequence[Problem] = (),
    message: str | None = None,
) -> DraftTestsPage:
    view = None
    if assembled is not None:
        view = AssembledView(
            instructions=assembled.instructions,
            prompt=assembled.prompt,
            today=long_date(assembled.today),
            simulated=assembled.simulated is not None,
            examples=assembled.examples,
            prompt_version=assembled.spec.prompt_version,
            calendar=calendar_view(assembled.spec),
        )
    nav = draft_nav(draft)
    return DraftTestsPage(
        nav=nav,
        summary=draft_summary(draft),
        answers=shown_answers(answers, assembled, scored),
        day=day,
        assembled=view,
        scored=scored_view(scored) if scored is not None else None,
        problems=problems,
        message=message,
        recent=_rows(nav, recent),
        keep=KEEP_TESTS,
        days=TEST_DAYS,
    )


# --- a publicação -----------------------------------------------------------------------------

NO_TEST_NEEDED = (
    "A publicação não precisa de teste: só mudaram partes que não vão ao LLM (textos da página, "
    "consentimento, PDF, prévia ou exemplos)."
)
TEST_COVERS = "O teste com sucesso de {when} cobre o que vai ao LLM: {changes}."
TEST_MISSING_TEXT = "Falta um teste com sucesso: {changes}. Teste o rascunho com o LLM."
FIRST_TEST_COVERS = "O teste com sucesso de {when} cobre o que vai ao LLM na primeira publicação."
FIRST_TEST_MISSING = (
    "Falta um teste com sucesso: na primeira publicação, tudo o que vai ao LLM é novo. Teste o "
    "rascunho com o LLM."
)
CONSENT_RAISED = (
    "O método, texto ou canais do consentimento mudaram: a versão dele sobe para {version}. Quem estiver com a página "
    "aberta vai concordar de novo antes de enviar."
)


@dataclass(frozen=True)
class PublishPage:
    """A publicação do rascunho: o que muda, o que ela pede e o que falta."""

    nav: DraftNav
    token: str
    number: int  # a versão que a publicação cria
    nothing: bool  # o rascunho está igual à versão publicada
    current_version: int | None  # None: a primeira publicação, que põe o formulário no ar
    changes: list[SectionChange]  # só as seções que mudaram (nenhuma na primeira publicação)
    problems: Sequence[Problem]
    gate: str | None
    gate_ok: bool
    contact: ContactInPromptView | None
    consent: str | None
    reasons: Sequence[str]  # o que faltou na última tentativa
    note: str
    note_max_length: int
    message: str | None
    dropped: tuple[DroppedValue, ...]  # os valores publicados que a versão nova não grava mais
    stale_assets: tuple[str, ...] = ()  # as imagens trocadas desde a versão publicada


def _gate(found: Review) -> tuple[str | None, bool]:
    """O que a publicação pede do teste, em texto (a regra é do serviço: `Review.test_status`)."""
    if found.nothing:
        return None, True
    if found.test_status == TEST_NOT_NEEDED:
        return NO_TEST_NEEDED, True
    changes = changed(found.llm_changes)
    if found.test_status == TEST_MISSING:
        return (
            FIRST_TEST_MISSING if found.first else TEST_MISSING_TEXT.format(changes=changes)
        ), False
    assert found.tested_at is not None  # coberto: há um teste com sucesso
    when = moment(found.tested_at)
    covers = FIRST_TEST_COVERS if found.first else TEST_COVERS
    return covers.format(changes=changes, when=when.text if when else "agora"), True


def publish_page(
    found: Review,
    *,
    reasons: Sequence[str] = (),
    problems: Sequence[Problem] | None = None,
    note: str = "",
    message: str | None = None,
) -> PublishPage:
    draft = found.draft
    gate, gate_ok = _gate(found)
    return PublishPage(
        nav=draft_nav(draft),
        token=draft.token,
        number=found.number,
        nothing=found.nothing,
        current_version=draft.current.number if draft.current is not None else None,
        changes=[] if found.first else [change for change in found.changes if change.changed],
        problems=found.problems if problems is None else problems,
        gate=gate,
        gate_ok=gate_ok,
        contact=contact_roles(found.contact) if found.contact else None,
        consent=CONSENT_RAISED.format(version=found.consent_version)
        if found.consent_version
        else None,
        reasons=reasons,
        note=note,
        note_max_length=NOTE_MAX_LENGTH,
        message=message,
        dropped=found.dropped_values(),
        stale_assets=draft.stale_assets,
    )


# --- a comparação -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CompareOption:
    value: str
    label: str


@dataclass(frozen=True)
class ComparePage:
    """Duas versões (ou uma versão e o rascunho) comparadas seção por seção."""

    nav: DraftNav
    options: list[CompareOption]
    old: str
    new: str
    old_label: str
    new_label: str
    sections: tuple[SectionChange, ...]
    changed: int  # quantas seções mudaram


def _label(ref: str) -> str:
    return "o rascunho" if ref == DRAFT_REF else f"a versão {ref}"


def compare_page(
    draft: Draft,
    numbers: Sequence[int],
    *,
    old: str,
    new: str,
    sections: tuple[SectionChange, ...],
) -> ComparePage:
    options = [CompareOption(str(number), f"Versão {number}") for number in numbers]
    if draft.pending:
        options.insert(0, CompareOption(DRAFT_REF, "Rascunho"))
    return ComparePage(
        nav=draft_nav(draft),
        options=options,
        old=old,
        new=new,
        old_label=_label(old),
        new_label=_label(new),
        sections=sections,
        changed=sum(1 for section in sections if section.changed),
    )
