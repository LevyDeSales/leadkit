"""O editor estruturado do rascunho no admin: a página pública (título, introdução e tempo), os
avisos das páginas de espera e de falha, as etapas, as perguntas e as opções, a identidade da lead,
o consentimento e as respostas de exemplo, em campos, no lugar do JSON (ADR 0010: Jinja2 e HTMX).

O estado do editor mora todo no formulário da página. Cada campo tem por nome o caminho dele na
definição (`steps.0.questions.1.label`), e é por esse nome que um problema da compilação volta para
o campo. Incluir, remover e reordenar não grava nada: o botão manda o formulário inteiro com a
operação (`op`), e a página volta com a mudança aplicada, inteira sem JavaScript ou só o trecho
das etapas com o HTMX. Só "Salvar o rascunho" compila e grava (`app.forms.drafts.save`).

- `FormEdit.of_document` lê a definição do rascunho; `FormEdit.of_form`, o formulário que voltou.
- `FormEdit.apply` aplica uma operação e diz qual campo recebe o foco.
- `FormEdit.screen` e `FormEdit.example_answers` montam as partes da definição que o editor cobre.
  Ele escreve só o que difere do padrão do formato.
- O modelo do plano (com as fases com datas), os dados do formulário e a pontuação têm editor
  próprio (`app.admin.plan_editor`, `app.admin.data_editor` e `app.admin.scoring_editor`), que o
  `FormEdit` carrega em `plan`, `data` e `scoring`. As operações deles começam por `plan-`,
  `data-` e `scoring-`, e `FormEdit.section_of` diz de qual trecho da página elas mexem.
- O aviso de cada página (`notices.waiting`, `notices.failed`) tem quatro campos, com o caminho da
  definição por nome (`notices.waiting.link.url`): título, texto, rótulo e endereço do link. Os
  quatro em branco: a página sai sem aviso, e a chave não vai para a definição. Um aviso começado
  vai como está, e a compilação aponta o campo que falta.
- Uma pergunta sem nome tira o nome do rótulo, e uma opção sem valor tira o valor do rótulo, sem
  repetir os que já existem.
- A identidade aponta para a pergunta, e não para o nome: renomear ou mover uma pergunta não a
  desfaz. No formulário, ela vai como a posição da pergunta (`etapa.pergunta`).
- A resposta de exemplo fica em cada pergunta: um texto (`example`), ou as opções marcadas
  (`example_option`, pela posição da opção).
- As condições (`show_when`) de uma pergunta ou opção têm editor próprio
  (`app.admin.condition_editor`), que o `FormEdit` carrega em `show_when` de cada pergunta e opção.
  As operações dele (`add-condition`, `add-rule`, `remove-rule`...) entram em `FormEdit.apply`.
- Os pontos de cada opção (`points`) são da pontuação: em branco, a opção não tem pontos. A vírgula
  vale como ponto decimal. O resto da pontuação (notas, faixas e regras de prioridade e de persona)
  tem editor próprio (`app.admin.scoring_editor`), que o `FormEdit` carrega em `scoring`, com o
  painel da pontuação salva ao lado (`app.admin.draft_pages`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from app.admin.condition_editor import (
    ConditionEdit,
    Place,
    RuleEdit,
    base_name,
    conditions_of_form,
    owners_of,
    targets_before,
)
from app.admin.condition_editor import (
    field_id as condition_id,
)
from app.admin.data_editor import DataEdit
from app.admin.plan_editor import PlanEdit
from app.admin.scoring_editor import ChoiceName, ScoringEdit, points_of
from app.forms import CONTACT_KINDS, IDENTITY_ROLES
from app.forms.definition.document import (
    MAX_CONDITION_RULES,
    Problem,
    identifier_of,
    reserved_name,
)
from app.forms.definition.questions import (
    CHOICE_KINDS,
    EMAIL_MAX_LENGTH,
    PHONE_MAX_LENGTH,
    TEXT_MAX_LENGTH,
)

# Os campos de toda pergunta e os de cada tipo: o que a página mostra, o que vai para a definição
# e onde um problema da compilação aparece (um problema noutro campo vai para o topo da pergunta).
# A ordem dos tipos é a da lista na página.
COMMON_FIELDS = frozenset({"name", "kind", "label", "help", "required", "contact", "example"})
_TEXT_ANSWER_FIELDS = frozenset({"placeholder", "autocomplete", "max_length"})
KIND_FIELDS = {
    "text": _TEXT_ANSWER_FIELDS | {"min_length"},
    "textarea": _TEXT_ANSWER_FIELDS | {"min_length"},
    "email": _TEXT_ANSWER_FIELDS,
    "tel": _TEXT_ANSWER_FIELDS,
    "choice": frozenset({"options"}),
    "choices": frozenset({"options", "min_choices", "max_choices"}),
}
KINDS = tuple(KIND_FIELDS)
NUMBER_FIELDS = ("min_length", "max_length", "min_choices", "max_choices")
# O tamanho máximo de uma resposta de texto que fica em branco no editor: o do texto e do texto
# longo o editor escreve na definição, que exige um; o do e-mail e do telefone é o do formato.
BLANK_MAX_LENGTH = {**TEXT_MAX_LENGTH, "email": EMAIL_MAX_LENGTH, "tel": PHONE_MAX_LENGTH}
NAME_MAX_LENGTH = 40  # o nome tirado do rótulo; o formato aceita até 64
CHANNELS = ("email", "whatsapp")

# Os campos de texto de uma pergunta, como o formulário os manda.
QUESTION_TEXTS = (
    "name",
    "kind",
    "label",
    "help",
    "placeholder",
    "autocomplete",
    "min_length",
    "max_length",
    "min_choices",
    "max_choices",
)
_STEP = re.compile(r"steps\.(\d{1,3})\.(title|help)")
_QUESTION = re.compile(r"steps\.(\d{1,3})\.questions\.(\d{1,3})\.([a-z_]+)")
_OPTION = re.compile(
    r"steps\.(\d{1,3})\.questions\.(\d{1,3})\.options\.(\d{1,3})\.(value|label|points)"
)
_POSITION = re.compile(r"(\d{1,3})\.(\d{1,3})")
_OP = re.compile(
    r"(add|remove|up|down|refresh)-(step|question|option|condition|rule|operator)"
    r"((?::\d{1,3}){0,4})"
)
_INTEGER = re.compile(r"-?\d{1,9}")


class FormFields(Protocol):
    """O formulário que voltou do navegador (o `FormData` do Starlette): os nomes dos campos, e
    os valores de cada um."""

    def __iter__(self) -> Iterator[str]: ...

    def getlist(self, key: str) -> list[Any]: ...


def field_id(name: str) -> str:
    """O id do campo na página, pelo nome dele (`steps.0.title` -> `f-steps-0-title`)."""
    return "f-" + name.replace(".", "-")


def _lines(text: str) -> str:
    """O texto como o editor o guardou: o navegador manda as quebras de linha como CRLF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(text: str) -> int | str | None:
    """Um número do formulário: inteiro, None em branco, ou o texto como veio, para a compilação
    apontar o problema no campo."""
    text = text.strip()
    if not text:
        return None
    return int(text) if _INTEGER.fullmatch(text) else text


def _value_of(label: str) -> str:
    """O valor de uma opção tirado do rótulo, com `_` entre as palavras (`identifier_of`)."""
    return identifier_of(label, separator="_", max_length=NAME_MAX_LENGTH)


def _name_of(label: str) -> str:
    """O nome de uma pergunta tirado do rótulo: começa por letra e não esconde um atributo do
    modelo das respostas (`reserved_name`)."""
    name = _value_of(label)
    if name and not name[0].isalpha():
        name = f"p_{name}"
    if reserved_name(name):
        name = f"{name}_resposta"
    return name


def _unique(name: str, taken: set[str]) -> str:
    found, count = name, 2
    while found in taken:
        found, count = f"{name}_{count}", count + 1
    taken.add(found)
    return found


# --- o editor ---------------------------------------------------------------------------------


@dataclass(eq=False)
class OptionEdit:
    value: str = ""
    label: str = ""
    points: str = ""  # os pontos da pontuação; em branco, a opção não tem
    example: bool = False  # marcada na resposta de exemplo
    show_when: ConditionEdit | None = None  # a condição da opção (`app.admin.condition_editor`)

    def document(self) -> dict[str, Any]:
        found: dict[str, Any] = {"value": self.value.strip(), "label": self.label.strip()}
        points = points_of(self.points)
        if points is not None:
            found["points"] = points
        if self.show_when is not None:
            found["show_when"] = self.show_when.document()
        return found


@dataclass(eq=False)
class QuestionEdit:
    """Uma pergunta como o editor a mostra: os textos como a operadora escreveu."""

    name: str = ""
    kind: str = "text"
    label: str = ""
    help: str = ""
    placeholder: str = ""
    autocomplete: str = ""
    required: bool = True
    contact: bool = False
    min_length: str = ""
    max_length: str = ""
    min_choices: str = ""
    max_choices: str = ""
    example: str = ""  # a resposta de exemplo, nos tipos de texto
    show_when: ConditionEdit | None = None  # a condição da pergunta
    options: list[OptionEdit] = field(default_factory=list)

    @property
    def choice(self) -> bool:
        return self.kind in CHOICE_KINDS

    @property
    def fields(self) -> frozenset[str]:
        """Os campos que a pergunta tem no editor, pelo tipo dela."""
        return COMMON_FIELDS | KIND_FIELDS.get(self.kind, frozenset())

    @property
    def always_contact(self) -> bool:
        """E-mail e telefone são dado de contato de todo jeito: a caixa não aparece."""
        return self.kind in CONTACT_KINDS

    @property
    def blank_max_length(self) -> int | None:
        """O tamanho máximo da resposta quando o campo fica em branco."""
        return BLANK_MAX_LENGTH.get(self.kind)

    def document(self) -> dict[str, Any]:
        """A pergunta no formato da definição, só com os campos do tipo e o que difere do
        padrão."""
        shown = self.fields
        found: dict[str, Any] = {
            "name": self.name.strip(),
            "kind": self.kind,
            "label": self.label.strip(),
        }
        texts = {"help": _lines(self.help)}
        texts |= {
            key: getattr(self, key) for key in ("placeholder", "autocomplete") if key in shown
        }
        found |= {key: value.strip() for key, value in texts.items() if value.strip()}
        if not self.required:
            found["required"] = False
        if self.contact:
            found["contact"] = True
        if self.show_when is not None:
            found["show_when"] = self.show_when.document()
        for key in NUMBER_FIELDS:
            value = _number(getattr(self, key)) if key in shown else None
            if value is None and key == "max_length":
                value = TEXT_MAX_LENGTH.get(self.kind)
            if value is not None:
                found[key] = value
        if "options" in shown:
            found["options"] = [option.document() for option in self.options]
        return found

    def example_answer(self) -> Any:
        """A resposta de exemplo; None se a pergunta fica sem resposta no exemplo."""
        if self.kind == "choice":
            return next((o.value.strip() for o in self.options if o.example), None)
        if self.kind == "choices":
            chosen = [option.value.strip() for option in self.options if option.example]
            return chosen or None
        return _lines(self.example).strip() or None


@dataclass(eq=False)
class StepEdit:
    title: str = ""
    help: str = ""
    questions: list[QuestionEdit] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        found: dict[str, Any] = {"title": self.title.strip()}
        help_text = _lines(self.help).strip()
        if help_text:
            found["help"] = help_text
        found["questions"] = [question.document() for question in self.questions]
        return found


@dataclass(eq=False)
class ConsentEdit:
    processing: str = ""
    notice: str = ""
    # O texto de cada canal de marketing e se o formulário o oferece.
    texts: dict[str, str] = field(default_factory=lambda: dict.fromkeys(CHANNELS, ""))
    offered: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(CHANNELS, False))

    def document(self) -> dict[str, Any]:
        """O consentimento sem a versão, que a publicação sobe sozinha."""
        return {
            "processing": _lines(self.processing).strip(),
            "notice": _lines(self.notice).strip(),
            "marketing": {
                channel: _lines(self.texts[channel]).strip()
                for channel in CHANNELS
                if self.offered[channel]
            },
        }


# As páginas da lead que levam um aviso extra (`notices` da definição).
NOTICE_PAGES = ("waiting", "failed")


@dataclass(eq=False)
class NoticeEdit:
    """O aviso de uma página como o editor o mostra: os textos como a operadora escreveu."""

    title: str = ""
    text: str = ""
    link_label: str = ""
    link_url: str = ""

    def document(self) -> dict[str, Any] | None:
        """O aviso no formato da definição, só com o que foi escrito; None se está tudo em branco
        (a página sai sem aviso). O que falta num aviso começado fica para a compilação apontar."""
        found: dict[str, Any] = {
            key: shown
            for key, value in (("title", self.title), ("text", self.text))
            if (shown := _lines(value).strip())
        }
        link = {
            key: shown
            for key, value in (("label", self.link_label), ("url", self.link_url))
            if (shown := value.strip())
        }
        if link:
            found["link"] = link
        return found or None


@dataclass(eq=False)
class NoticesEdit:
    """Os avisos das páginas de espera e de falha, pelo nome da página (`NOTICE_PAGES`)."""

    pages: dict[str, NoticeEdit] = field(
        default_factory=lambda: {page: NoticeEdit() for page in NOTICE_PAGES}
    )

    @classmethod
    def of_document(cls, notices: Any) -> NoticesEdit:
        found = notices if isinstance(notices, Mapping) else {}
        pages = {}
        for page in NOTICE_PAGES:
            notice = found.get(page)
            notice = notice if isinstance(notice, Mapping) else {}
            link = notice.get("link")
            link = link if isinstance(link, Mapping) else {}
            pages[page] = NoticeEdit(
                title=_text(notice.get("title")),
                text=_text(notice.get("text")),
                link_label=_text(link.get("label")),
                link_url=_text(link.get("url")),
            )
        return cls(pages)

    @classmethod
    def of_form(cls, form: FormFields) -> NoticesEdit:
        def first(key: str) -> str:
            values = form.getlist(key)
            return _lines(str(values[0])) if values else ""

        return cls(
            {
                page: NoticeEdit(
                    title=first(f"notices.{page}.title"),
                    text=first(f"notices.{page}.text"),
                    link_label=first(f"notices.{page}.link.label"),
                    link_url=first(f"notices.{page}.link.url"),
                )
                for page in NOTICE_PAGES
            }
        )

    def document(self) -> dict[str, Any] | None:
        """O bloco `notices` da definição; None se nenhuma página tem aviso."""
        found = {page: notice.document() for page, notice in self.pages.items()}
        return {page: notice for page, notice in found.items() if notice is not None} or None


# Os textos sugeridos para um canal que o formulário ainda não oferece.
CHANNEL_DEFAULTS = {
    "email": "Quero receber dicas e novidades da Leadkit por e-mail.",
    "whatsapp": "Quero receber dicas e novidades da Leadkit pelo WhatsApp.",
}


@dataclass(eq=False)
class CompletionEdit:
    """Conclusão como texto de formulário; a validação final pertence ao compilador."""

    mode: str = "result"
    message: str = ""
    button_label: str = ""
    url: str = ""
    receiver_id: str = ""
    receiver_revision: str = ""

    @classmethod
    def of_document(cls, value: Any) -> CompletionEdit:
        found = value if isinstance(value, Mapping) else {}
        return cls(
            **{key: _text(found.get(key)) for key in ("message", "button_label", "url")},
            mode=_text(found.get("mode")) or "result",
            receiver_id=_text((found.get("handoff") or {}).get("receiver_id")),
            receiver_revision=_text((found.get("handoff") or {}).get("receiver_revision")),
        )

    @classmethod
    def of_form(cls, form: FormFields) -> CompletionEdit:
        return cls.of_document(
            {
                key: form.getlist(f"completion.{key}")[0]
                for key in ("mode", "message", "button_label", "url")
                if form.getlist(f"completion.{key}")
            }
            | {
                "handoff": {
                    key: (form.getlist(f"completion.handoff.{key}") or [""])[0]
                    for key in ("receiver_id", "receiver_revision")
                }
            }
        )

    def document(self) -> dict[str, Any]:
        return {
            "mode": self.mode.strip(),
            **(
                {
                    "handoff": {
                        "receiver_id": self.receiver_id.strip(),
                        "receiver_revision": int(self.receiver_revision)
                        if self.receiver_revision.isdecimal()
                        else self.receiver_revision,
                    }
                }
                if self.receiver_id.strip() or self.receiver_revision.strip()
                else {}
            ),
            **{
                key: _lines(value).strip()
                for key, value in (
                    ("message", self.message),
                    ("button_label", self.button_label),
                    ("url", self.url),
                )
                if value.strip()
            },
        }


@dataclass(eq=False)
class FormEdit:
    """O que o editor estruturado cobre de uma definição, como a operadora o edita."""

    # As operações do editor das etapas, perguntas, opções e condições, no nome do campo `op` sem os
    # números (`add-question`, `up-option`...): saem da tabela `_OPERATIONS`, no fim do módulo, e o
    # teste de cobertura da API de admin cobra uma linha da matriz para cada uma. As de `plan-`,
    # `data-` e `scoring-` são dos editores delas.
    OPERATIONS: ClassVar[tuple[str, ...]] = ()

    generation: str = "pdf"
    release_mode: str = "automatic"
    title: str = ""
    intro: str = ""
    minutes: str = ""
    steps: list[StepEdit] = field(default_factory=list)
    identity: dict[str, QuestionEdit | None] = field(default_factory=dict)
    consent: ConsentEdit = field(default_factory=ConsentEdit)
    plan: PlanEdit = field(default_factory=PlanEdit)
    data: DataEdit = field(default_factory=DataEdit)
    scoring: ScoringEdit = field(default_factory=ScoringEdit)
    notices: NoticesEdit = field(default_factory=NoticesEdit)
    completion: CompletionEdit = field(default_factory=CompletionEdit)

    # --- leitura ------------------------------------------------------------------------------

    @classmethod
    def of_document(cls, document: Mapping[str, Any]) -> FormEdit:
        """O editor de uma definição guardada (que compila: a de um rascunho ou de uma versão)."""
        examples = document.get("examples")
        answers = examples.get("answers") if isinstance(examples, Mapping) else None
        answers = answers if isinstance(answers, Mapping) else {}
        steps = [
            StepEdit(
                title=_text(step.get("title")),
                help=_text(step.get("help")),
                questions=[
                    _question_of(question, answers) for question in step.get("questions", ())
                ],
            )
            for step in document.get("steps", ())
        ]
        by_name = {question.name: question for step in steps for question in step.questions}
        identity = document.get("identity") or {}
        consent = document.get("consent") or {}
        marketing = consent.get("marketing") or {}
        return cls(
            generation=_text(document.get("generation", "pdf")),
            title=_text(document.get("title")),
            intro=_text(document.get("intro")),
            minutes=_text(document.get("minutes")),
            steps=steps,
            identity={role: by_name.get(identity.get(role) or "") for role in IDENTITY_ROLES},
            consent=ConsentEdit(
                processing=_text(consent.get("processing")),
                notice=_text(consent.get("notice")),
                texts={c: _text(marketing.get(c)) or CHANNEL_DEFAULTS[c] for c in CHANNELS},
                offered={channel: bool(marketing.get(channel)) for channel in CHANNELS},
            ),
            plan=PlanEdit.of_document(document.get("plan"), document.get("phases")),
            data=DataEdit.of_document(document.get("data")),
            scoring=ScoringEdit.of_document(document.get("scoring")),
            notices=NoticesEdit.of_document(document.get("notices")),
            completion=CompletionEdit.of_document(document.get("completion")),
            release_mode=(document.get("release") or {}).get("mode", "automatic"),
        )

    @classmethod
    def of_form(cls, form: FormFields) -> FormEdit:
        """O editor como o formulário da página voltou. Os índices dizem a ordem; um campo que não
        é do editor fica de fora."""

        def first(key: str) -> str:
            values = form.getlist(key)
            return _lines(str(values[0])) if values else ""

        steps: dict[int, StepEdit] = {}
        questions: dict[tuple[int, int], QuestionEdit] = {}
        options: dict[tuple[int, int], dict[int, OptionEdit]] = {}
        for key in form:
            if found := _STEP.fullmatch(key):
                step = steps.setdefault(int(found[1]), StepEdit())
                setattr(step, found[2], first(key))
            elif found := _OPTION.fullmatch(key):
                of_question = options.setdefault((int(found[1]), int(found[2])), {})
                setattr(of_question.setdefault(int(found[3]), OptionEdit()), found[4], first(key))
            elif found := _QUESTION.fullmatch(key):
                question = questions.setdefault(
                    (int(found[1]), int(found[2])), QuestionEdit(required=False)
                )
                if found[3] in ("required", "contact"):
                    setattr(question, found[3], True)
                elif found[3] in QUESTION_TEXTS:
                    setattr(question, found[3], first(key))
        for place in options:
            questions.setdefault(place, QuestionEdit(required=False))
        for (i, j), question in questions.items():
            steps.setdefault(i, StepEdit())
            ordered = sorted(options.get((i, j), {}).items())
            question.options = [option for _, option in ordered]
            # A opção marcada vai pela posição dela na página, num campo à parte do texto: trocar
            # o tipo entre texto e escolha começa a resposta de exemplo de novo.
            if question.choice:
                marked = form.getlist(f"steps.{i}.questions.{j}.example_option")
                indexes = {str(value) for value in marked}
                for k, option in ordered:
                    option.example = str(k) in indexes
            else:
                question.example = first(f"steps.{i}.questions.{j}.example")
        for (i, j, k), condition in conditions_of_form(form).items():
            if question := questions.get((i, j)):
                holder = question if k is None else options.get((i, j), {}).get(k)
                if holder is not None:
                    holder.show_when = condition
        for (i, _), question in sorted(questions.items(), key=lambda item: item[0]):
            steps[i].questions.append(question)
        edit = cls(
            generation=first("generation") or "pdf",
            title=first("title"),
            intro=first("intro"),
            minutes=first("minutes"),
            steps=[steps[i] for i in sorted(steps)],
            consent=ConsentEdit(
                processing=first("consent.processing"),
                notice=first("consent.notice"),
                texts={channel: first(f"consent.marketing.{channel}") for channel in CHANNELS},
                offered={
                    channel: bool(form.getlist(f"consent.marketing.{channel}.offered"))
                    for channel in CHANNELS
                },
            ),
            plan=PlanEdit.of_form(form),
            data=DataEdit.of_form(form),
            scoring=ScoringEdit.of_form(form),
            notices=NoticesEdit.of_form(form),
            completion=CompletionEdit.of_form(form),
            release_mode=first("release.mode") or "automatic",
        )
        for role in IDENTITY_ROLES:
            position = _POSITION.fullmatch(first(f"identity.{role}"))
            chosen = (int(position[1]), int(position[2])) if position else None
            edit.identity[role] = questions.get(chosen) if chosen else None
        edit._fill_names()
        return edit

    def _fill_names(self) -> None:
        """A pergunta sem nome tira o nome do rótulo; a opção sem valor, o valor do rótulo."""
        names = {q.name.strip() for step in self.steps for q in step.questions if q.name.strip()}
        for step in self.steps:
            for question in step.questions:
                if not question.name.strip() and question.label.strip():
                    name = _name_of(question.label)
                    question.name = _unique(name, names) if name else ""
                values = {o.value.strip() for o in question.options if o.value.strip()}
                for option in question.options:
                    if not option.value.strip() and option.label.strip():
                        value = _value_of(option.label)
                        option.value = _unique(value, values) if value else ""

    def question_at(self, i: int, j: int) -> QuestionEdit | None:
        """A pergunta `j` da etapa `i`; None se uma das duas não existe."""
        if 0 <= i < len(self.steps) and 0 <= j < len(self.steps[i].questions):
            return self.steps[i].questions[j]
        return None

    def choice_names(self) -> tuple[ChoiceName, ...]:
        """As perguntas de escolha com nome, com as opções e os pontos que a página tem agora: o
        que uma fórmula da pontuação lê."""
        return tuple(
            ChoiceName(
                name=question.name.strip(),
                label=question.label.strip(),
                multiple=question.kind == "choices",
                options=tuple(
                    (option.value.strip(), option.points.strip()) for option in question.options
                ),
            )
            for step in self.steps
            for question in step.questions
            if question.choice and question.name.strip()
        )

    def path_of(self, name: str) -> str | None:
        """O caminho da pergunta com este nome (`steps.0.questions.1`); None se não há."""
        for i, step in enumerate(self.steps):
            for j, question in enumerate(step.questions):
                if question.name.strip() == name:
                    return f"steps.{i}.questions.{j}"
        return None

    # --- o que vai para a definição -----------------------------------------------------------

    def screen(self) -> dict[str, Any]:
        """A página, as etapas, a identidade e o consentimento (sem a versão) para a definição."""
        identity = {
            role: question.name.strip()
            for role, question in self.identity.items()
            if question is not None
        }
        found: dict[str, Any] = {
            "title": self.title.strip(),
            "intro": _lines(self.intro).strip(),
        }
        minutes = _number(self.minutes)
        if minutes is not None:
            found["minutes"] = minutes
        found |= {
            "steps": [step.document() for step in self.steps],
            "identity": identity,
            "consent": self.consent.document(),
            "plan": self.plan.document(),
            "data": self.data.document(),
        }
        if self.generation != "pdf":
            found["generation"] = self.generation
            found.pop("plan")
        phases = self.plan.phases.document() if self.generation == "pdf" else None
        if phases is not None:
            found["phases"] = phases
        if (scoring := self.scoring.document()) is not None:
            found["scoring"] = scoring
        if (notices := self.notices.document()) is not None:
            found["notices"] = notices
        if self.release_mode != "automatic":
            found["release"] = {"mode": self.release_mode}
        completion = self.completion.document()
        if completion != {"mode": "result"}:
            found["completion"] = completion
        return found

    def problems(self) -> list[Problem]:
        """O que o editor não consegue escrever na definição: salvar para aqui, antes de compilar."""
        found = self.data.problems() + (self.plan.problems() if self.generation == "pdf" else [])
        for i, step in enumerate(self.steps):
            for j, question in enumerate(step.questions):
                for place, condition in owners_of(question, i, j):
                    if condition is not None:
                        found += condition.problems(base_name(place))
        return found

    def example_answers(self) -> dict[str, Any]:
        """As respostas de exemplo, uma por pergunta respondida no exemplo."""
        found: dict[str, Any] = {}
        for step in self.steps:
            for question in step.questions:
                answer = question.example_answer()
                if answer is not None and question.name.strip():
                    found[question.name.strip()] = answer
        return found

    # --- as operações -------------------------------------------------------------------------

    def apply(self, op: str) -> str | None:
        """Aplica uma operação do editor (`add-question:1`, `up-option:0:2:1`...) e devolve o id do
        campo que recebe o foco. Uma operação desconhecida, ou de uma página velha que aponta para
        o que não existe mais, não muda nada."""
        found = _OP.fullmatch(op)
        if found is None:
            if op.startswith("plan-"):
                return self.plan.apply(op)
            if op.startswith("scoring-"):
                return self.scoring.apply(op)
            return self.data.apply(op) if op.startswith("data-") else None
        numbers = [int(number) for number in found[3].split(":")[1:]]
        operation = _OPERATIONS.get((found[1], found[2]))
        if operation is None:
            return None
        try:
            return operation(self, *numbers)
        except (IndexError, TypeError):
            return None

    @staticmethod
    def section_of(op: str) -> str:
        """O trecho da página que a operação muda: `plan`, `data`, `scoring` ou `structure` (as
        etapas)."""
        for section in ("plan", "data", "scoring"):
            if op.startswith(f"{section}-"):
                return section
        return "structure"

    def _forget(self, gone: list[QuestionEdit]) -> None:
        """A identidade não aponta para uma pergunta que saiu."""
        for role, question in self.identity.items():
            if question is not None and any(question is item for item in gone):
                self.identity[role] = None

    def _add_step(self) -> str:
        self.steps.append(StepEdit(questions=[QuestionEdit()]))
        return field_id(f"steps.{len(self.steps) - 1}.title")

    def _remove_step(self, i: int) -> str:
        self._forget(self.steps.pop(i).questions)
        return ADD_STEP_ID

    def _move_step(self, i: int, delta: int) -> str:
        j = i + delta
        if 0 <= j < len(self.steps) and i >= 0:
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            i = j
        return field_id(f"steps.{i}.title")

    def _add_question(self, i: int) -> str:
        questions = self.steps[i].questions
        questions.append(QuestionEdit())
        return field_id(f"steps.{i}.questions.{len(questions) - 1}.label")

    def _remove_question(self, i: int, j: int) -> str:
        self._forget([self.steps[i].questions.pop(j)])
        return add_question_id(i)

    def _move_question(self, i: int, j: int, delta: int) -> str:
        """Sobe ou desce a pergunta na etapa; na ponta, ela passa para a etapa vizinha."""
        questions = self.steps[i].questions
        question = questions[j]
        k = j + delta
        if 0 <= k < len(questions):
            questions[j], questions[k] = questions[k], question
        elif delta < 0 and i > 0:
            questions.pop(j)
            self.steps[i - 1].questions.append(question)
            i, k = i - 1, len(self.steps[i - 1].questions) - 1
        elif delta > 0 and i + 1 < len(self.steps):
            questions.pop(j)
            self.steps[i + 1].questions.insert(0, question)
            i, k = i + 1, 0
        else:
            k = j
        return field_id(f"steps.{i}.questions.{k}.label")

    def _refresh_question(self, i: int, j: int) -> str | None:
        """O tipo da pergunta mudou: ela volta aberta, com os campos do tipo novo e o foco no
        tipo."""
        return field_id(f"steps.{i}.questions.{j}.kind") if self.question_at(i, j) else None

    def _add_option(self, i: int, j: int) -> str:
        options = self.steps[i].questions[j].options
        options.append(OptionEdit())
        return field_id(f"steps.{i}.questions.{j}.options.{len(options) - 1}.label")

    def _remove_option(self, i: int, j: int, k: int) -> str:
        self.steps[i].questions[j].options.pop(k)
        return add_option_id(i, j)

    def _move_option(self, i: int, j: int, k: int, delta: int) -> str:
        options = self.steps[i].questions[j].options
        option = options[k]
        m = k + delta
        if 0 <= m < len(options) and k >= 0:
            options[k], options[m] = options[m], option
            k = m
        return field_id(f"steps.{i}.questions.{j}.options.{k}.label")

    # --- as condições ---------------------------------------------------------------------------
    # As operações de condição dependem de quantos números levam (ver `app.admin.condition_editor`):
    # a da pergunta leva `i:j`; a da opção, `i:j:k`; e as de uma regra levam o índice dela no fim.

    def _holder(self, i: int, j: int, k: int | None) -> QuestionEdit | OptionEdit:
        """A pergunta, ou a opção `k` dela, que tem a condição."""
        question = self.steps[i].questions[j]
        return question if k is None else question.options[k]

    def _add_condition(self, *numbers: int) -> str | None:
        i, j, k = _owner(numbers)
        holder = self._holder(i, j, k)
        if holder.show_when is not None:
            return None
        holder.show_when = ConditionEdit(rules=[RuleEdit()])
        return condition_id(f"{base_name((i, j, k))}.rules.0.field")

    def _remove_condition(self, *numbers: int) -> str:
        i, j, k = _owner(numbers)
        self._holder(i, j, k).show_when = None
        return condition_id(f"{base_name((i, j, k))}.add_condition")

    def _add_rule(self, *numbers: int) -> str | None:
        i, j, k = _owner(numbers)
        condition = self._holder(i, j, k).show_when
        if condition is None or len(condition.rules) >= MAX_CONDITION_RULES:
            return None
        condition.rules.append(RuleEdit())
        return condition_id(f"{base_name((i, j, k))}.rules.{len(condition.rules) - 1}.field")

    def _remove_rule(self, *numbers: int) -> str:
        i, j, k, r = _rule_owner(numbers)
        holder = self._holder(i, j, k)
        condition = holder.show_when
        if condition is None:
            raise IndexError(r)
        condition.rules.pop(r)
        if not condition.rules:  # a condição não vive sem regra
            holder.show_when = None
            return condition_id(f"{base_name((i, j, k))}.add_condition")
        return condition_id(f"{base_name((i, j, k))}.add_rule")

    def _rule_at(self, numbers: tuple[int, ...]) -> tuple[Place, RuleEdit]:
        i, j, k, r = _rule_owner(numbers)
        condition = self._holder(i, j, k).show_when
        if condition is None:
            raise IndexError(r)
        return (i, j, k), condition.rules[r]

    def _refresh_rule(self, *numbers: int) -> str:
        """A pergunta da regra mudou: a regra se ajusta a ela e o foco fica na pergunta."""
        place, rule = self._rule_at(numbers)
        rule.retarget(targets_before(self, place[0], place[1]).get(rule.question))
        return condition_id(f"{base_name(place)}.rules.{numbers[-1]}.field")

    def _refresh_operator(self, *numbers: int) -> str:
        """O operador da regra mudou: o valor ganha a forma dele e o foco fica no operador."""
        place, rule = self._rule_at(numbers)
        rule.reshape()
        return condition_id(f"{base_name(place)}.rules.{numbers[-1]}.operator")


ADD_STEP_ID = "op-add-step"


def add_question_id(i: int) -> str:
    return f"op-add-question-{i}"


def add_option_id(i: int, j: int) -> str:
    return f"op-add-option-{i}-{j}"


_OPERATIONS: dict[tuple[str, str], Callable[..., str | None]] = {
    ("add", "step"): FormEdit._add_step,
    ("remove", "step"): FormEdit._remove_step,
    ("up", "step"): lambda edit, i: edit._move_step(i, -1),
    ("down", "step"): lambda edit, i: edit._move_step(i, 1),
    ("add", "question"): FormEdit._add_question,
    ("remove", "question"): FormEdit._remove_question,
    ("up", "question"): lambda edit, i, j: edit._move_question(i, j, -1),
    ("down", "question"): lambda edit, i, j: edit._move_question(i, j, 1),
    ("refresh", "question"): FormEdit._refresh_question,
    ("add", "option"): FormEdit._add_option,
    ("remove", "option"): FormEdit._remove_option,
    ("up", "option"): lambda edit, i, j, k: edit._move_option(i, j, k, -1),
    ("down", "option"): lambda edit, i, j, k: edit._move_option(i, j, k, 1),
    ("add", "condition"): FormEdit._add_condition,
    ("remove", "condition"): FormEdit._remove_condition,
    ("add", "rule"): FormEdit._add_rule,
    ("remove", "rule"): FormEdit._remove_rule,
    ("refresh", "rule"): FormEdit._refresh_rule,
    ("refresh", "operator"): FormEdit._refresh_operator,
}
FormEdit.OPERATIONS = tuple(f"{verb}-{noun}" for verb, noun in _OPERATIONS)


def _owner(numbers: tuple[int, ...]) -> tuple[int, int, int | None]:
    """A pergunta (`i:j`) ou a opção (`i:j:k`) de uma operação de condição."""
    if len(numbers) == 2:
        return numbers[0], numbers[1], None
    if len(numbers) == 3:
        return numbers[0], numbers[1], numbers[2]
    raise TypeError(numbers)


def _rule_owner(numbers: tuple[int, ...]) -> tuple[int, int, int | None, int]:
    """A regra de uma pergunta (`i:j:r`) ou de uma opção (`i:j:k:r`)."""
    if len(numbers) == 3:
        return numbers[0], numbers[1], None, numbers[2]
    if len(numbers) == 4:
        return numbers[0], numbers[1], numbers[2], numbers[3]
    raise TypeError(numbers)


def _question_of(document: Mapping[str, Any], answers: Mapping[str, Any]) -> QuestionEdit:
    kind = str(document.get("kind", "text"))
    answer = answers.get(_text(document.get("name")))
    chosen = set(answer) if isinstance(answer, list) else {answer}
    return QuestionEdit(
        name=_text(document.get("name")),
        kind=kind,
        label=_text(document.get("label")),
        help=_text(document.get("help")),
        placeholder=_text(document.get("placeholder")),
        autocomplete=_text(document.get("autocomplete")),
        required=bool(document.get("required", True)),
        contact=bool(document.get("contact", False)),
        min_length=_text(document.get("min_length")),
        max_length=_text(document.get("max_length")),
        min_choices=_text(document.get("min_choices")),
        max_choices=_text(document.get("max_choices")),
        show_when=ConditionEdit.of_document(document.get("show_when")),
        example="" if kind in CHOICE_KINDS or answer is None else _text(answer),
        options=[
            OptionEdit(
                value=_text(option.get("value")),
                label=_text(option.get("label")),
                points=_text(option.get("points")),
                example=kind in CHOICE_KINDS and option.get("value") in chosen,
                show_when=ConditionEdit.of_document(option.get("show_when")),
            )
            for option in document.get("options") or ()
        ],
    )
