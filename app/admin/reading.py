"""A leitura de uma versão de formulário no admin: o que a lead vê e o que vai ao LLM, montado do
contrato (`FormSpec`), que é o que o motor usa.

A definição guardada da mesma versão, lida pelo mesmo modelo do compilador (`FormDocument`), traz o
que o contrato não guarda: os campos internos do plano, os dados do formulário, o texto dos
templates e as respostas de exemplo.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.admin.condition_editor import sentences_of
from app.forms import FormSpec, Notice, Option, Question, answer_fields, identity_fields
from app.forms.definition.document import FormDocument, PlanNodeDoc, RuleDoc, WindowsBlock
from app.forms.definition.phases import linked_options
from app.forms.definition.plan import range_sentence, text_nodes
from app.forms.definition.questions import CHOICE_KINDS, questions_of
from app.web.formview import steps_view

QUESTION_KINDS = {
    "text": "texto",
    "email": "e-mail",
    "tel": "telefone",
    "textarea": "texto longo",
    "choice": "escolha única",
    "choices": "escolha múltipla",
}
IDENTITY_ROLES = (("email", "E-mail"), ("name", "Nome"), ("phone", "Telefone"))
# O tipo de um campo do plano no schema, por extenso, e o plural dele nos itens de uma lista.
SCHEMA_KINDS = {
    "string": ("texto", "textos"),
    "object": ("grupo", "grupos"),
    "integer": ("número inteiro", "números inteiros"),
    "number": ("número", "números"),
    "boolean": ("sim ou não", "sim ou não"),
}
OPTIONS_KIND = ("opções fixas", "opções fixas")


def _inline(sentence: str | None) -> str | None:
    """A frase do limite no meio de uma linha: com minúscula e sem o ponto."""
    return sentence[0].lower() + sentence[1:].rstrip(".") if sentence else None


# --- etapas e perguntas -----------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionReading:
    name: str
    label: str
    kind: str  # por extenso
    required: bool
    help: str | None
    placeholder: str | None
    contact: bool  # dado de contato: fica fora do rascunho guardado no aparelho
    limits: str | None
    options: tuple[Option, ...]
    appears: str | None  # quando a pergunta aparece, se tem condição (`show_when`)
    option_appears: Mapping[str, str]  # o mesmo, por valor de opção
    # Obrigatória que tem condição: o modelo das respostas só a exige quando ela aparece, e por
    # isso `required` é falso nela (`app.web.formview.QuestionView`).
    required_when_shown: bool = False

    @property
    def requirement(self) -> str:
        """Se a lead precisa responder, como a leitura diz."""
        if self.required:
            return "obrigatória"
        return "obrigatória quando aparece" if self.required_when_shown else "opcional"


@dataclass(frozen=True)
class StepReading:
    title: str
    help: str | None
    questions: tuple[QuestionReading, ...]


def _steps(spec: FormSpec, definition: FormDocument) -> tuple[StepReading, ...]:
    """As etapas da página pública, com o que o modelo das respostas exige de cada pergunta e a
    condição de cada pergunta e opção que a tem. As respostas que a tela não pede (um formulário só
    de API) ficam numa etapa à parte."""
    asked, offered = sentences_of(definition.steps)
    steps: list[StepReading] = []
    on_screen: set[str] = set()
    if spec.screen is not None:
        for step in steps_view(spec, spec.screen):
            questions = []
            for view in step.questions:
                question = view.question
                on_screen.add(question.name)
                limits = None
                if question.kind != "choice":
                    unit = ("opção", "opções") if question.kind == "choices" else None
                    singular, plural = unit or ("caractere", "caracteres")
                    sentence = range_sentence(view.min_length, view.max_length, singular, plural)
                    limits = _inline(sentence)
                questions.append(
                    QuestionReading(
                        name=question.name,
                        label=question.label,
                        kind=QUESTION_KINDS.get(question.kind, question.kind),
                        required=view.required,
                        help=question.help,
                        placeholder=question.placeholder,
                        contact=not view.draft,
                        limits=limits,
                        options=question.options,
                        appears=asked.get(question.name),
                        option_appears={
                            option.value: offered[question.name, option.value]
                            for option in question.options
                            if (question.name, option.value) in offered
                        },
                        required_when_shown=view.required_when_shown,
                    )
                )
            steps.append(StepReading(step.title, step.help, tuple(questions)))
    rest = [
        QuestionReading(
            name=name,
            label=info.title or name,
            kind="valor",
            required=info.is_required(),
            help=info.description,
            placeholder=None,
            contact=False,
            limits=None,
            options=(),
            appears=None,
            option_appears={},
        )
        for name, info in answer_fields(spec.answers_model).items()
        if name not in on_screen
    ]
    if rest:
        steps.append(StepReading("Fora da tela: só pela API", None, tuple(rest)))
    return tuple(steps)


def _identity(spec: FormSpec) -> list[tuple[str, str | None]]:
    """Quais perguntas identificam a lead: o papel e a pergunta, ou None se o formulário não
    coleta aquele dado (`app.forms.identity_fields`)."""
    roles = identity_fields(spec)
    if roles is None:  # uma identidade que não só lê as respostas
        return [(label, "não deu para ler do contrato") for _, label in IDENTITY_ROLES]
    questions = {
        question.name: question
        for step in (spec.screen.steps if spec.screen else ())
        for question in step.questions
    }
    found: list[tuple[str, str | None]] = []
    for role, label in IDENTITY_ROLES:
        key = roles[role]
        if key is None:
            found.append((label, None))
            continue
        question: Question | None = questions.get(key)
        found.append((label, f"{question.label} ({key})" if question else key))
    return found


def _notices(spec: FormSpec) -> tuple[tuple[str, Notice], ...]:
    """Os avisos extras que a versão tem, cada um com a página em que aparece."""
    if spec.screen is None:
        return ()
    pages = (
        ("página de espera", spec.screen.notices.waiting),
        ("página de falha", spec.screen.notices.failed),
    )
    return tuple((page, notice) for page, notice in pages if notice is not None)


def _consent(spec: FormSpec) -> list[tuple[str, str]]:
    consent = spec.screen.consent if spec.screen is not None else None
    if consent is None:
        return [("Versão do texto", spec.consent_text_version)]
    absent = "não oferecido"
    return [
        ("Versão do texto", spec.consent_text_version),
        ("Autorização do processamento", consent.processing),
        ("Aviso sobre o uso dos dados", consent.notice),
        ("Novidades por e-mail", consent.marketing_email or absent),
        ("Novidades por WhatsApp", consent.marketing_whatsapp or absent),
    ]


# --- modelo do plano --------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanField:
    """Um campo do modelo do plano, como o LLM o recebe no schema e o servidor o valida."""

    name: str
    kind: str  # "texto", "lista de grupos", "opções fixas"...
    description: str | None
    limits: tuple[str, ...]
    values: tuple[str, ...]  # as opções fixas, do campo ou dos itens da lista
    internal: bool  # o campo não chega ao PDF nem à prévia
    optional: bool
    fields: tuple[PlanField, ...]  # os campos do grupo, ou dos itens de uma lista de grupos
    inherited_internal: bool = False


def _resolved(node: Mapping[str, Any], defs: Mapping[str, Any]) -> dict[str, Any]:
    """O nó do schema com o opcional (`anyOf` com `null`) desembrulhado e a referência a outro
    modelo (`$ref`) expandida. O que vem junto da referência, como a descrição, vale mais."""
    found = dict(node)
    options = [item for item in found.get("anyOf", ()) if item.get("type") != "null"]
    if len(options) == 1:
        del found["anyOf"]
        found = {**options[0], **found}
    ref = found.pop("$ref", None)
    if ref is not None:
        found = {**defs[ref.rsplit("/", 1)[-1]], **found}
    return found


def _kind(node: Mapping[str, Any]) -> tuple[str, str]:
    """O tipo do nó por extenso, no singular e no plural."""
    if "enum" in node:
        return OPTIONS_KIND
    found = str(node.get("type"))
    return SCHEMA_KINDS.get(found, (found, found))


def _text_limit(node: Mapping[str, Any]) -> str | None:
    return range_sentence(node.get("minLength"), node.get("maxLength"), "caractere", "caracteres")


def _plan_fields(
    node: Mapping[str, Any],
    defs: Mapping[str, Any],
    internal: frozenset[tuple[str, ...]],
    path: tuple[str, ...] = (),
) -> tuple[PlanField, ...]:
    required = set(node.get("required", ()))
    return tuple(
        _plan_field(name, child, defs, internal, (*path, name), name in required)
        for name, child in node.get("properties", {}).items()
    )


def _plan_field(
    name: str,
    raw: Mapping[str, Any],
    defs: Mapping[str, Any],
    internal: frozenset[tuple[str, ...]],
    path: tuple[str, ...],
    required: bool,
) -> PlanField:
    node = _resolved(raw, defs)
    kind = _kind(node)[0]
    limits = [_text_limit(node)]
    values = tuple(node.get("enum", ()))
    shape = node  # o nó que tem os campos: o do grupo, ou o dos itens de uma lista
    if node.get("type") == "array":
        shape = _resolved(node.get("items", {}), defs)
        kind = f"lista de {_kind(shape)[1]}"
        limits = [range_sentence(node.get("minItems"), node.get("maxItems"), "item", "itens")]
        each = _inline(_text_limit(shape))
        if each:
            limits.append(f"Cada item: {each}.")
        values = tuple(shape.get("enum", ()))
    fields = _plan_fields(shape, defs, internal, path) if shape.get("type") == "object" else ()
    description = (node.get("description") or "").strip()
    return PlanField(
        name=name,
        kind=kind,
        description=description or None,
        # A descrição já termina com os limites, na mesma frase.
        limits=tuple(limit for limit in limits if limit and limit not in description),
        values=values,
        internal=path in internal,
        inherited_internal=any(path[:size] in internal for size in range(1, len(path))),
        optional=not required,
        fields=fields,
    )


def _internal_paths(
    fields: Sequence[PlanNodeDoc], path: tuple[str, ...] = ()
) -> Iterator[tuple[str, ...]]:
    """Os campos internos da árvore do plano de uma definição, pelo caminho dos nomes (os itens
    de uma lista não têm nome: os campos deles seguem o da lista)."""
    for field in fields:
        here = (*path, field.name or "")
        if field.internal:
            yield here
        holder = field.items if field.kind == "list" and field.items is not None else field
        yield from _internal_paths(holder.fields or (), here)


def _plan(spec: FormSpec, definition: FormDocument) -> tuple[str | None, tuple[PlanField, ...]]:
    """A descrição do modelo do plano e os campos dele, na ordem em que o LLM escreve."""
    schema = spec.plan_model.model_json_schema()
    internal = frozenset(_internal_paths(definition.plan.fields))
    description = (schema.get("description") or "").strip() or None
    return description, _plan_fields(schema, schema.get("$defs", {}), internal)


def _and(items: Sequence[object]) -> str:
    """ "30", "30 e 30", "30, 30 e 30"."""
    names = [str(item) for item in items]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} e {names[-1]}"


def _phase_checks(definition: FormDocument) -> list[str]:
    """As fases com datas por extenso: quantas são e quanto dura cada uma, de onde vêm as janelas e
    em que fases cabe cada valor ligado a uma janela."""
    phases = definition.phases
    if phases is None:
        return []
    count = len(phases.days)
    found = [
        f"O plano tem {count} {'fase' if count == 1 else 'fases'} de {_and(phases.days)} "
        f"{'dia' if phases.days == [1] else 'dias'}, a primeira a partir do dia em que o plano é "
        "gerado, em São Paulo."
        + (f" As janelas vêm do bloco {phases.windows}." if phases.windows else "")
    ]
    block = definition.data.get(phases.windows or "")
    labels = (
        {window.name: window.label or window.name for window in block.windows}
        if isinstance(block, WindowsBlock)
        else {}
    )
    for _, trail, node in linked_options(definition.plan):
        rules = [
            f"{value} só cabe nas fases em que a janela {labels.get(window, window)} está aberta"
            for value, window in (node.in_window or {}).items()
        ]
        found.append(
            f"{'.'.join(trail)}: {'; '.join(rules)}. Os outros valores cabem em qualquer fase."
        )
    return found


def _plan_checks(definition: FormDocument) -> tuple[str, ...]:
    """As validações declarativas do plano por extenso: as expressões proibidas de cada regra, com
    onde valem, o padrão proibido de cada campo e as fases com datas. Os limites de tamanho e de
    parágrafos já estão na descrição de cada campo."""
    plan = definition.plan
    found = []
    for rule in plan.forbidden:
        expressions = ", ".join(f"«{expression}»" for expression in rule.expressions)
        where = {
            "all": "Em todos os textos do plano",
            "only": f"Só em {', '.join(rule.fields)}",
            "except": f"Em todos os textos menos em {', '.join(rule.fields)}",
        }[rule.scope]
        found.append(f"{where}: sem {expressions}.")
    for path, node in text_nodes(plan.fields):
        if node.forbidden_pattern is not None:
            message = node.forbidden_pattern.message
            shown = f" ({message})" if message else ""
            found.append(
                f"{path}: o texto não pode ter o padrão {node.forbidden_pattern.pattern}{shown}"
            )
    return tuple(found + _phase_checks(definition))


# --- templates, dados e exemplos --------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """Um texto da versão, como está escrito: um template ou um bloco de dados."""

    title: str
    text: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _templates(definition: FormDocument) -> tuple[Source, ...]:
    found = [
        ("PDF (HTML)", definition.pdf.html),
        ("PDF (CSS)", definition.pdf.css),
        ("Prévia na página de resultado", definition.preview),
    ]
    return tuple(Source(title, text) for title, text in found if text.strip())


def _data(definition: FormDocument) -> tuple[Source, ...]:
    """Os dados do formulário: os blocos da definição, como estão escritos."""
    return tuple(
        Source(name, _json(block.model_dump(mode="json")))
        for name, block in definition.data.items()
    )


# --- a pontuação ------------------------------------------------------------------------------


def shown_number(value: Any) -> str:
    """Um valor da pontuação como a operadora o lê: o inteiro como está e o decimal com até quatro
    casas e vírgula."""
    if isinstance(value, bool) or not isinstance(value, float):
        return str(value)
    return f"{value:.4f}".rstrip("0").rstrip(".").replace(".", ",")


@dataclass(frozen=True)
class LevelReading:
    start: str  # "a partir de 90", ou "o resto" no último nível
    key: str
    label: str


@dataclass(frozen=True)
class NoteReading:
    name: str
    formula: str
    key: str | None  # o nome do nível da faixa na pontuação
    label: str | None  # o nome do rótulo da faixa
    levels: tuple[LevelReading, ...]


@dataclass(frozen=True)
class RuleReading:
    when: str | None  # None: o "senão", que sempre vale
    result: str  # "P2", "Comercial" ou "a resposta de principal_desafio"


@dataclass(frozen=True)
class PointsReading:
    question: str  # o nome da pergunta
    label: str
    options: tuple[tuple[str, str], ...]  # (rótulo da opção, pontos); sem pontos: "—"


@dataclass(frozen=True)
class ScoringReading:
    """A pontuação declarada de uma versão: as notas e as faixas, as regras de prioridade e de
    persona, na ordem em que valem, e os pontos de cada opção."""

    notes: tuple[NoteReading, ...]
    priority: tuple[RuleReading, ...]
    persona: tuple[RuleReading, ...]
    points: tuple[PointsReading, ...]


def _rule(rule: RuleDoc) -> RuleReading:
    result = rule.value if rule.answer is None else f"a resposta de {rule.answer}"
    return RuleReading(rule.when, result or "")


def _scoring(definition: FormDocument) -> ScoringReading | None:
    scoring = definition.scoring
    if scoring is None:
        return None
    notes = []
    for score in scoring.scores:
        bands = score.bands
        levels = (
            tuple(
                LevelReading(
                    f"a partir de {shown_number(level.min)}"
                    if level.min is not None
                    else "o resto",
                    level.key,
                    level.label,
                )
                for level in bands.levels
            )
            if bands is not None
            else ()
        )
        notes.append(
            NoteReading(
                name=score.name,
                formula=score.formula,
                key=bands.key if bands is not None else None,
                label=bands.label if bands is not None else None,
                levels=levels,
            )
        )
    points = tuple(
        PointsReading(
            question.name,
            question.label,
            tuple(
                (option.label, "—" if option.points is None else shown_number(option.points))
                for option in question.options or ()
            ),
        )
        for question in questions_of(definition.steps)
        if question.kind in CHOICE_KINDS
        and any(option.points is not None for option in question.options or ())
    )
    return ScoringReading(
        notes=tuple(notes),
        priority=tuple(_rule(rule) for rule in scoring.priority),
        persona=tuple(_rule(rule) for rule in scoring.persona),
        points=points,
    )


# --- a leitura --------------------------------------------------------------------------------


@dataclass(frozen=True)
class FormReading:
    generation: str
    title: str  # o da versão: o que a lead vê na página e, se aplicável, no PDF
    intro: str | None
    minutes: int | None
    steps: tuple[StepReading, ...]
    identity: list[tuple[str, str | None]]  # (papel, pergunta); None: não coletado
    consent: list[tuple[str, str]]
    instructions: str | None  # como vão ao LLM
    prompt: str | None  # o template
    prompt_version: str
    scoring: ScoringReading | None  # a pontuação declarada; None: sem pontuação
    plan_description: str | None
    plan: tuple[PlanField, ...] | None
    plan_checks: tuple[str, ...]  # o que o servidor confere no plano, além do schema
    data: tuple[Source, ...]
    templates: tuple[Source, ...]
    example_answers: str
    example_plan: str | None
    notices: tuple[tuple[str, Notice], ...] = ()  # os avisos da espera e da falha, com a página


def reading_of(spec: FormSpec, definition: FormDocument) -> FormReading:
    """A leitura da versão do contrato `spec`. `definition` é a definição guardada da mesma
    versão, já lida pelo compilador (`parse`)."""
    generates = spec.generation == "pdf"
    description, plan = _plan(spec, definition) if generates else (None, None)
    return FormReading(
        generation=spec.generation,
        title=spec.title,
        intro=spec.screen.intro if spec.screen is not None else None,
        minutes=spec.screen.minutes if spec.screen is not None else None,
        steps=_steps(spec, definition),
        identity=_identity(spec),
        consent=_consent(spec),
        instructions=spec.instructions,
        prompt=spec.prompt_template,
        prompt_version=spec.prompt_version,
        scoring=_scoring(definition),
        plan_description=description,
        plan=plan,
        plan_checks=_plan_checks(definition) if generates else (),
        data=_data(definition),
        templates=_templates(definition) if generates else (),
        example_answers=_json(definition.examples.answers),
        example_plan=_json(spec.example_plan) if generates else None,
        notices=_notices(spec),
    )
