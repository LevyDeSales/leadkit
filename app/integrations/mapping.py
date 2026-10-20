"""O mapeamento de uma integração: qual dado do envio preenche cada campo da tabela.

Tudo aqui é função pura, sem banco nem rede, e nenhum id de campo ou de opção do Baserow mora no
código: o que o código conhece são os tipos de campo (`SUPPORTED_TYPES`) e as fontes dos dados
(`Source`); o resto vem da tabela da operadora e do formulário.

- **Fontes** (`catalog`): cada resposta do formulário (`answer:<pergunta>`), os dados da lead (nome,
  e-mail, telefone) e os do envio (o código do pedido, a versão, os links, a prioridade, os
  consentimentos, o rastreamento e as respostas em JSON). Uma fonte entra em um campo de tipo
  compatível (`compatible`).
- **Opções de seleção**: o valor de uma pergunta de escolha casa com a opção do campo do Baserow
  pelo **rótulo** que a lead viu, sem diferença de maiúsculas, acentos, espaços e travessões
  (`normalize`). `pair_options` mostra, para o relatório, o que casou e o que não casou; a linha
  que traz uma opção sem par não é enviada (`UnmatchedOption`).
- **Relatório** (`build_report`): o que a operadora confere antes de ativar. Uma integração só ativa
  com o relatório limpo.
- **Linha** (`build_row`): os valores prontos para a API, pela chave `field_<id>`.

Um texto digitado pela lead nunca entra numa mensagem de erro daqui: as exceções citam a pergunta e
o campo, que são configuração.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.forms import FormSpec, Option, Question
from app.integrations.baserow import BaserowField, BaserowOption

TEXT_TYPES = ("text", "long_text", "email", "phone_number", "url")
SELECT_TYPES = ("single_select", "multiple_select")
SUPPORTED_TYPES = (*TEXT_TYPES, "boolean", *SELECT_TYPES)
KEY_TYPES = ("text", "long_text")  # o campo do código do pedido: o filtro "igual" funciona neles
TYPE_LABELS = {
    "text": "texto",
    "long_text": "texto longo",
    "email": "e-mail",
    "phone_number": "telefone",
    "url": "link",
    "boolean": "sim ou não",
    "single_select": "seleção única",
    "multiple_select": "seleção múltipla",
}

KEY_SOURCE = "job.public_id"
CAPTURE_KEY_SOURCE = "submission.id"
MappingProfile = Literal["pdf", "capture"]
ANSWER_PREFIX = "answer:"
SourceKind = Literal["text", "json", "flag", "choice", "choices"]
SAMPLE_EMAIL = "teste@example.test"
SAMPLE_TEXT = "Texto de exemplo"


@dataclass(frozen=True)
class Source:
    """De onde vem o valor de um campo. `kind` diz o formato: `text`, `json` (um texto com o JSON),
    `flag` (sim ou não), `choice` (uma opção de pergunta) ou `choices` (várias). `aliases` são nomes
    de campo que sugerem esta fonte quando a operadora ainda não mapeou nada."""

    key: str
    label: str
    group: str
    kind: SourceKind
    aliases: tuple[str, ...] = ()


LEAD = "Dados da lead"
SUBMISSION = "Dados do envio"
ANSWERS = "Respostas do formulário"

STATIC_SOURCES: tuple[Source, ...] = (
    Source("lead.name", "Nome", LEAD, "text", ("nome", "name", "nome completo")),
    Source("lead.email", "E-mail", LEAD, "text", ("e-mail", "email", "e mail")),
    Source("lead.phone", "Telefone", LEAD, "text", ("telefone", "phone", "whatsapp", "celular")),
    Source(
        KEY_SOURCE,
        "Código do pedido (a chave da sincronização)",
        SUBMISSION,
        "text",
        ("codigo", "codigo do pedido", "job", "id do pedido", "public id", "pedido"),
    ),
    Source("job.form_version", "Versão do formulário", SUBMISSION, "text", ("versao",)),
    Source("job.pdf_url", "Link do PDF", SUBMISSION, "text", ("pdf", "link do pdf", "url do pdf")),
    Source("job.page_url", "Link da página do plano", SUBMISSION, "text", ("pagina do plano",)),
    Source("job.priority", "Prioridade", SUBMISSION, "text", ("prioridade",)),
    Source("job.persona", "Persona", SUBMISSION, "text", ("persona",)),
    Source("job.answers_json", "Todas as respostas (JSON)", SUBMISSION, "json", ("respostas",)),
    Source("job.consent_json", "Consentimento (JSON)", SUBMISSION, "json", ("consentimento",)),
    Source("job.tracking_json", "Rastreamento (JSON)", SUBMISSION, "json", ("tracking", "utm")),
    Source("job.scores_json", "Pontuação (JSON)", SUBMISSION, "json", ("pontuacao",)),
    Source("consent.privacy", "Consentiu o tratamento dos dados", SUBMISSION, "flag"),
    Source("consent.marketing_email", "Aceitou marketing por e-mail", SUBMISSION, "flag"),
    Source("consent.marketing_whatsapp", "Aceitou marketing por WhatsApp", SUBMISSION, "flag"),
)


CAPTURE_SOURCES: tuple[Source, ...] = tuple(
    Source(
        CAPTURE_KEY_SOURCE
        if source.key == KEY_SOURCE
        else source.key.replace("job.", "submission.", 1),
        "Código da submissão (a chave da sincronização)"
        if source.key == KEY_SOURCE
        else source.label,
        source.group,
        source.kind,
        ("codigo da submissao", "submission", "id da submissao")
        if source.key == KEY_SOURCE
        else source.aliases,
    )
    for source in STATIC_SOURCES
    if source.key not in ("job.pdf_url", "job.page_url")
)


def _static_sources(profile: MappingProfile) -> tuple[Source, ...]:
    if profile == "pdf":
        return STATIC_SOURCES
    if profile == "capture":
        return CAPTURE_SOURCES
    raise ValueError("Perfil de mapeamento desconhecido.")


def normalize(text: str) -> str:
    """O texto sem acentos, em minúsculas, com os travessões como hífen e os espaços colapsados:
    a forma em que o rótulo de uma opção e o de uma opção do Baserow se comparam."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    plain = re.sub(r"[‐‑‒–—−]", "-", plain)
    return " ".join(plain.casefold().split())


def questions_of(spec: FormSpec | None) -> list[Question]:
    """As perguntas da tela da versão, na ordem; vazio para uma versão sem tela."""
    if spec is None or spec.screen is None:
        return []
    return [question for step in spec.screen.steps for question in step.questions]


def answer_source(question: Question) -> Source:
    kind: SourceKind = (
        "choice"
        if question.kind == "choice"
        else "choices"
        if question.kind == "choices"
        else "text"
    )
    return Source(
        f"{ANSWER_PREFIX}{question.name}",
        question.label,
        ANSWERS,
        kind,
        (question.name.replace("_", " "),),
    )


def catalog(spec: FormSpec | None, *, profile: MappingProfile = "pdf") -> dict[str, Source]:
    """As fontes disponíveis na versão e no perfil; o default conserva as fontes PDF."""
    sources = {source.key: source for source in _static_sources(profile)}
    for question in questions_of(spec):
        source = answer_source(question)
        sources[source.key] = source
    return sources


def grouped(sources: Mapping[str, Source]) -> list[tuple[str, list[Source]]]:
    """As fontes por grupo, na ordem em que a tela as oferece: dados da lead, do envio e as
    respostas."""
    order = (LEAD, SUBMISSION, ANSWERS)
    return [
        (group, [source for source in sources.values() if source.group == group])
        for group in order
        if any(source.group == group for source in sources.values())
    ]


def compatible(source: Source, field_type: str) -> bool:
    """A fonte pode preencher um campo deste tipo: qualquer uma preenche um campo de texto, um
    sim ou não vem de um consentimento, e uma seleção vem de uma pergunta de escolha."""
    if field_type in TEXT_TYPES:
        return True
    if field_type == "boolean":
        return source.kind == "flag"
    if field_type == "single_select":
        return source.kind == "choice"
    if field_type == "multiple_select":
        return source.kind in ("choice", "choices")
    return False


def unsupported_kind(live: BaserowField) -> Literal["read_only", "unsupported_type"] | None:
    """Por que a integração não preenche este campo, como um código (`read_only`: o Baserow o
    calcula; `unsupported_type`: o tipo não está em `SUPPORTED_TYPES`), ou None se preenche."""
    if live.read_only:
        return "read_only"
    if live.type not in SUPPORTED_TYPES:
        return "unsupported_type"
    return None


def reason_text(kind: str | None, field_type: str) -> str | None:
    """O motivo de `unsupported_kind`, por extenso, para a tela e as mensagens."""
    if kind == "read_only":
        return "campo de somente leitura"
    if kind == "unsupported_type":
        return f"tipo «{field_type}» sem suporte"
    return None


def unsupported_reason(live: BaserowField) -> str | None:
    """Por que a integração não preenche este campo, ou None se preenche."""
    return reason_text(unsupported_kind(live), live.type)


def suggest(live: BaserowField, sources: Mapping[str, Source]) -> str:
    """A fonte que o nome do campo sugere ("" se nenhuma): o nome igual ao de uma pergunta, ao
    rótulo dela ou a um dos apelidos de uma fonte, sem diferença de acentos e de maiúsculas. Só
    sugere uma fonte compatível com o tipo do campo, e só quando a leitura é inequívoca. Um campo
    "Nome" serve à lead e à pergunta do nome: vale o dado da lead."""
    if unsupported_reason(live) is not None:
        return ""
    name = normalize(live.name)
    found = [
        source
        for source in sources.values()
        if compatible(source, live.type)
        and name in {normalize(source.label), *(normalize(alias) for alias in source.aliases)}
    ]
    if len(found) > 1:
        found = [source for source in found if source.group != ANSWERS]
    return found[0].key if len(found) == 1 else ""


# --- as entradas do mapeamento ---------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """Um campo da tabela e a fonte que o preenche. `name` e `type` são os do campo quando o
    mapeamento foi salvo: a tela os mostra sem consultar o Baserow, e a sincronização confere se mudaram."""

    field_id: int
    name: str
    type: str
    source: str

    def as_json(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "name": self.name,
            "type": self.type,
            "source": self.source,
        }


def entries_of(stored: Sequence[Mapping[str, Any]]) -> list[Entry]:
    """As entradas guardadas na configuração. Uma entrada malformada é ignorada."""
    entries = []
    for item in stored:
        field_id, name, kind, source = (item.get(k) for k in ("field_id", "name", "type", "source"))
        if (
            isinstance(field_id, int)
            and isinstance(name, str)
            and isinstance(kind, str)
            and isinstance(source, str)
        ):
            entries.append(Entry(field_id, name, kind, source))
    return entries


def key_entry(entries: Sequence[Entry], *, profile: MappingProfile = "pdf") -> Entry | None:
    """A entrada da chave do perfil: por ela a sincronização acha a linha de volta."""
    key = CAPTURE_KEY_SOURCE if profile == "capture" else KEY_SOURCE
    _static_sources(profile)
    return next((entry for entry in entries if entry.source == key), None)


class MappingError(Exception):
    """O mapeamento não serve para esta sincronização. A mensagem é para a tela e para o histórico
    dela: cita campos e perguntas, que são configuração, e nunca um valor da lead."""


class UnmatchedOption(MappingError):
    """A resposta tem uma opção que o campo de seleção do Baserow não tem."""


class InvalidMapping(ValueError):
    """O mapeamento que a operadora mandou não serve; `problems` diz o que corrigir."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def check_entries(
    entries: Sequence[Entry],
    live: Sequence[BaserowField],
    sources: Mapping[str, Source],
    *,
    profile: MappingProfile = "pdf",
) -> None:
    """Confere um mapeamento novo antes de guardá-lo; levanta `InvalidMapping` com o que corrigir.
    Cada campo existe na tabela, é preenchível e recebe uma fonte que o formulário oferece e que
    serve ao tipo dele, e um campo de texto recebe a chave do perfil (pedido ou submissão)."""
    allowed = {source.key for source in _static_sources(profile)}
    sources = {
        key: value
        for key, value in sources.items()
        if key in allowed or key.startswith(ANSWER_PREFIX)
    }
    key_source = CAPTURE_KEY_SOURCE if profile == "capture" else KEY_SOURCE
    key_label = "código da submissão" if profile == "capture" else "código do pedido"
    by_id = {item.id: item for item in live}
    problems: list[str] = []
    if profile == "capture" and len({entry.field_id for entry in entries}) != len(entries):
        problems.append("Escolha somente uma fonte para cada campo da captura.")
    for entry in entries:
        found = by_id.get(entry.field_id)
        if found is None:
            problems.append(f"O campo «{entry.name}» não existe na tabela.")
            continue
        reason = unsupported_reason(found)
        if reason is not None:
            problems.append(f"O campo «{found.name}» não pode ser preenchido: {reason}.")
            continue
        source = sources.get(entry.source)
        if source is None:
            problems.append(f"«{found.name}» recebe uma fonte que o formulário não tem.")
        elif not compatible(source, found.type):
            problems.append(
                f"«{found.name}» é um campo de {TYPE_LABELS[found.type]} e não recebe "
                f"«{source.label}»."
            )
    keys = [entry for entry in entries if entry.source == key_source]
    if len(keys) != 1:
        problems.append(
            f"Escolha um campo, e só um, para o {key_label}: é por ele que a sincronização acha "
            "a linha de volta."
        )
    elif (found := by_id.get(keys[0].field_id)) is not None and found.type not in KEY_TYPES:
        problems.append(
            f"O campo do {key_label} («{found.name}») precisa ser de texto ou de texto longo."
        )
    if problems:
        raise InvalidMapping(problems)


# --- opções de seleção -----------------------------------------------------------------------


@dataclass(frozen=True)
class OptionPair:
    """Uma opção do formulário e a opção do Baserow que ela alcança (None: não casou)."""

    value: str
    label: str
    baserow: BaserowOption | None


def pair_options(options: Sequence[Option], choices: Sequence[BaserowOption]) -> list[OptionPair]:
    """Cada opção do formulário com a opção do Baserow de mesmo rótulo: igual ao pé da letra, senão
    igual depois de `normalize`. Duas opções do formulário podem alcançar a mesma do Baserow."""
    exact = {choice.value: choice for choice in choices}
    loose: dict[str, BaserowOption] = {}
    for choice in choices:
        loose.setdefault(normalize(choice.value), choice)
    return [
        OptionPair(
            option.value,
            option.label,
            exact.get(option.label) or loose.get(normalize(option.label)),
        )
        for option in options
    ]


def match_value(
    value: str, question: Question | None, choices: Sequence[BaserowOption]
) -> BaserowOption | None:
    """A opção do Baserow para o valor gravado numa resposta. O rótulo vem da versão do formulário
    que a lead respondeu; um valor que essa versão não conhece casa pelo próprio texto."""
    label = value
    if question is not None:
        label = next((o.label for o in question.options if o.value == value), value)
    return next(iter(pair_options([Option(value, label)], choices))).baserow


# --- o relatório de antes de ativar ----------------------------------------------------------


@dataclass(frozen=True)
class FieldReport:
    """Um campo mapeado no relatório. Nos campos de seleção, `pairs` traz cada opção da pergunta
    com a opção do Baserow que ela alcança."""

    entry: Entry
    source: str  # o rótulo da fonte
    pairs: tuple[OptionPair, ...] = ()

    @property
    def unmatched(self) -> list[OptionPair]:
        return [pair for pair in self.pairs if pair.baserow is None]


@dataclass(frozen=True)
class Report:
    """O que a operadora confere antes de ativar: um problema que impede a sincronização, ou uma opção
    que não casou. `ok` quando não há nenhum dos dois."""

    fields: tuple[FieldReport, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def unmatched(self) -> int:
        return sum(len(item.unmatched) for item in self.fields)

    @property
    def ok(self) -> bool:
        return bool(self.fields) and not self.problems and self.unmatched == 0


def build_report(
    entries: Sequence[Entry],
    live: Sequence[BaserowField],
    spec: FormSpec | None,
    *,
    profile: MappingProfile = "pdf",
) -> Report:
    """O relatório do mapeamento contra a tabela como ela está agora e a versão atual do
    formulário."""
    sources = catalog(spec, profile=profile)
    questions = {question.name: question for question in questions_of(spec)}
    by_id = {item.id: item for item in live}
    problems: list[str] = []
    fields: list[FieldReport] = []
    if profile == "capture" and len({entry.field_id for entry in entries}) != len(entries):
        problems.append("Escolha somente uma fonte para cada campo da captura.")
    if not entries:
        problems.append("Nenhum campo está mapeado: escolha o que preenche cada campo da tabela.")
    elif key_entry(entries, profile=profile) is None:
        key_label = "código da submissão" if profile == "capture" else "código do pedido"
        problems.append(f"O {key_label} não está mapeado a nenhum campo.")
    else:
        key = key_entry(entries, profile=profile)
        if sum(entry.source == key.source for entry in entries) != 1:
            problems.append("Escolha somente um campo para a chave da sincronização.")
        if key.type not in KEY_TYPES:
            problems.append(
                "O campo da chave da sincronização precisa ser de texto ou de texto longo."
            )
    for entry in entries:
        found = by_id.get(entry.field_id)
        source = sources.get(entry.source)
        if found is None:
            problems.append(f"O campo «{entry.name}» não existe mais na tabela.")
            continue
        if found.type != entry.type:
            problems.append(
                f"O campo «{found.name}» mudou de tipo na tabela (era {entry.type}, agora "
                f"{found.type}): salve o mapeamento de novo."
            )
            continue
        reason = unsupported_reason(found)
        if reason is not None:
            problems.append(f"O campo «{found.name}» não pode ser preenchido: {reason}.")
            continue
        if source is None:
            problems.append(
                f"«{found.name}» recebe uma fonte que a versão atual do formulário não tem: "
                "escolha outra."
            )
            continue
        if not compatible(source, found.type):
            problems.append(f"«{found.name}» não recebe «{source.label}» neste tipo de campo.")
            continue
        pairs: tuple[OptionPair, ...] = ()
        if found.type in SELECT_TYPES and source.key.startswith(ANSWER_PREFIX):
            question = questions.get(source.key.removeprefix(ANSWER_PREFIX))
            if question is not None:
                pairs = tuple(pair_options(question.options, found.options))
        fields.append(FieldReport(entry, source.label, pairs))
    return Report(tuple(fields), tuple(problems))


# --- a linha ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RowData:
    """O que um envio entrega à montagem da linha, lido do banco (ou inventado, no teste).
    `questions` são as perguntas da versão que a lead respondeu, pelo nome."""

    public_id: str
    form_version: str
    lead_name: str | None
    lead_email: str
    lead_phone: str | None
    pdf_url: str
    page_url: str
    priority: str | None
    persona: str | None
    answers: dict[str, Any]
    consent: dict[str, Any]
    tracking: dict[str, Any]
    scores: dict[str, Any]
    questions: dict[str, Question] = field(default_factory=dict)


@dataclass(frozen=True)
class CaptureRowData:
    """Os dados históricos de uma captura, sem pedido ou capacidades de PDF inventados."""

    submission_id: str
    form_version: str
    lead_name: str | None
    lead_email: str
    lead_phone: str | None
    priority: str | None
    persona: str | None
    answers: dict[str, Any]
    consent: dict[str, Any]
    tracking: dict[str, Any]
    scores: dict[str, Any]
    questions: dict[str, Question] = field(default_factory=dict)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def raw_value(
    source: str, data: RowData | CaptureRowData, *, profile: MappingProfile = "pdf"
) -> Any:
    """O valor de uma fonte disponível; recusa fontes estranhas antes de converter ausências."""
    sources = {item.key for item in _static_sources(profile)}
    sources.update(f"{ANSWER_PREFIX}{name}" for name in data.questions)
    if source not in sources:
        raise MappingError("A fonte não está disponível nesta versão e perfil do formulário.")
    if profile == "capture" and not isinstance(data, CaptureRowData):
        raise MappingError("O perfil de captura exige dados de uma submissão.")
    if profile == "pdf" and not isinstance(data, RowData):
        raise MappingError("O perfil PDF exige dados de um pedido.")
    prefix = "submission" if profile == "capture" else "job"
    simple: dict[str, Any] = {
        "lead.name": data.lead_name,
        "lead.email": data.lead_email,
        "lead.phone": data.lead_phone,
        f"{prefix}.form_version": data.form_version,
        f"{prefix}.priority": data.priority,
        f"{prefix}.persona": data.persona,
        f"{prefix}.answers_json": _json(data.answers),
        f"{prefix}.consent_json": _json(data.consent),
        f"{prefix}.tracking_json": _json(data.tracking),
        f"{prefix}.scores_json": _json(data.scores),
        "consent.privacy": bool(data.consent.get("privacy_at") or data.consent.get("processing")),
        "consent.marketing_email": bool(data.consent.get("marketing_email")),
        "consent.marketing_whatsapp": bool(data.consent.get("marketing_whatsapp")),
    }
    if isinstance(data, CaptureRowData):
        simple[CAPTURE_KEY_SOURCE] = data.submission_id
    else:
        simple.update(
            {KEY_SOURCE: data.public_id, "job.pdf_url": data.pdf_url, "job.page_url": data.page_url}
        )
    if source in simple:
        return simple[source]
    return data.answers.get(source.removeprefix(ANSWER_PREFIX))


def _label_of(value: Any, question: Question | None) -> str:
    """O rótulo que a lead viu para o valor de uma opção; o próprio valor se a versão não o tem."""
    text = str(value)
    if question is None:
        return text
    return next((o.label for o in question.options if o.value == text), text)


def _as_text(source: Source, raw: Any, question: Question | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "Sim" if raw else "Não"
    if isinstance(raw, list):
        return ", ".join(_label_of(item, question) for item in raw)
    if source.kind in ("choice", "choices"):
        return _label_of(raw, question)
    return str(raw)


def _as_options(
    raw: Any, question: Question | None, choices: Sequence[BaserowOption], field_name: str
) -> list[int]:
    values = raw if isinstance(raw, list) else [] if raw in (None, "") else [raw]
    ids = []
    for value in values:
        found = match_value(str(value), question, choices)
        if found is None:
            where = f"a pergunta «{question.label}»" if question else "uma pergunta"
            raise UnmatchedOption(
                f"O campo «{field_name}» não tem a opção que a resposta trouxe para {where}."
            )
        ids.append(found.id)
    return ids


def build_row(
    entries: Sequence[Entry],
    live: Sequence[BaserowField],
    data: RowData | CaptureRowData,
    *,
    profile: MappingProfile = "pdf",
) -> dict[str, Any]:
    """Os valores da linha, pela chave `field_<id>` da API. Levanta `MappingError` se um campo
    mapeado não existe mais, mudou de tipo ou não recebe a fonte, e `UnmatchedOption` se uma
    resposta traz uma opção que o campo de seleção não tem."""
    by_id = {item.id: item for item in live}
    sources = {source.key: source for source in _static_sources(profile)}
    row: dict[str, Any] = {}
    if profile == "capture" and len({entry.field_id for entry in entries}) != len(entries):
        raise MappingError("Escolha somente uma fonte para cada campo da captura.")
    for entry in entries:
        found = by_id.get(entry.field_id)
        if found is None:
            raise MappingError(f"O campo «{entry.name}» não existe mais na tabela.")
        if found.type != entry.type:
            raise MappingError(f"O campo «{found.name}» mudou de tipo na tabela.")
        question = (
            data.questions.get(entry.source.removeprefix(ANSWER_PREFIX))
            if entry.source.startswith(ANSWER_PREFIX)
            else None
        )
        source = sources.get(entry.source)
        if source is None and question is not None:
            source = answer_source(question)
        if source is None:
            raise MappingError(
                f"O campo «{found.name}» recebe uma fonte indisponível nesta versão e perfil."
            )
        reason = unsupported_reason(found)
        if reason is not None:
            raise MappingError(f"O campo «{found.name}» não pode ser preenchido: {reason}.")
        if not compatible(source, found.type):
            raise MappingError(f"O campo «{found.name}» não recebe «{source.label}».")
        raw = raw_value(entry.source, data, profile=profile)
        key = f"field_{found.id}"
        if found.type in TEXT_TYPES:
            row[key] = _as_text(source, raw, question)
        elif found.type == "boolean":
            row[key] = bool(raw)
        elif found.type == "single_select":
            ids = _as_options(raw, question, found.options, found.name)
            row[key] = ids[0] if ids else None
        else:
            row[key] = _as_options(raw, question, found.options, found.name)
    return row


# --- o envio de teste ------------------------------------------------------------------------


def sample_answer(question: Question) -> Any:
    """Uma resposta inventada para a pergunta: a primeira opção (as duas primeiras, numa pergunta
    de várias), ou um texto de exemplo."""
    if question.kind == "choice" and question.options:
        return question.options[0].value
    if question.kind == "choices" and question.options:
        return [option.value for option in question.options[:2]]
    if question.kind == "email":
        return SAMPLE_EMAIL
    if question.kind == "tel":
        return "11999990000"
    return SAMPLE_TEXT


def sample_data(
    spec: FormSpec, public_id: str, *, profile: MappingProfile = "pdf"
) -> RowData | CaptureRowData:
    """Um envio sintético do formulário, com a lead de exemplo no domínio `exemplo`: nenhum dado de
    lead de verdade."""
    questions = {question.name: question for question in questions_of(spec)}
    _static_sources(profile)
    values = {
        "form_version": spec.version,
        "lead_name": "Contadora Exemplo",
        "lead_email": SAMPLE_EMAIL,
        "lead_phone": "11999990000",
        "priority": None,
        "persona": None,
        "answers": {name: sample_answer(question) for name, question in questions.items()},
        "consent": {
            "privacy_at": "2026-01-01T00:00:00+00:00",
            "text_version": spec.consent_text_version,
            "marketing_email": False,
            "marketing_whatsapp": False,
        },
        "tracking": {"utm_source": "teste"},
        "scores": {},
        "questions": questions,
    }

    if profile == "capture":
        return CaptureRowData(submission_id=public_id, **values)
    return RowData(
        public_id=public_id,
        pdf_url="https://example.test/plano-de-teste.pdf",
        page_url="https://example.test/plano-de-teste",
        **values,
    )
