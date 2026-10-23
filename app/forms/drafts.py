"""O rascunho de um formulário do banco, o teste dele, a publicação da versão seguinte e a criação de
um formulário novo no admin, do zero ou por cópia (ADR 0013).

Rascunho (`FormDraft`)
----------------------
- Um por formulário do banco, fora das versões publicadas, que não mudam. Enquanto a operadora não
  muda nada, o rascunho é a própria versão atual (`Draft.saved` falso); a linha nasce na primeira
  vez que ela salva uma mudança e sai quando ela salva de volta o que a versão publicada diz ou
  publica. "Diz o mesmo" é a forma normal (`app.forms.definition.normal`): o padrão do formato
  escrito por extenso (`"required": true`) numa e ausente na outra não é mudança. Por isso o aviso
  de rascunho pendente é "há uma linha que não diz o mesmo que a versão atual" (`Draft.edited`), e
  a lista, a página e a publicação concordam mesmo com uma linha gravada antes disso.
- Um formulário criado do zero ou duplicado (`create`) começa só com o rascunho, antes da versão 1
  (`Draft.current` None): o rascunho inteiro é pendente, e a primeira publicação cria a versão 1.
  Até ela, o endereço muda junto com o salvar (`save(new_slug=...)`); depois, fica fixo.
- O editor (`DraftTexts`) recebe as instruções e o prompt em texto, o resto da definição num JSON
  só e, do editor estruturado (`app.admin.form_editor`), a página, as etapas, a identidade, o
  consentimento e as respostas de exemplo. O JSON vem sem o slug e sem a versão do texto do
  consentimento, que a publicação sobe sozinha quando o texto muda, e sem o PDF e a prévia, que têm
  o editor deles (`TemplateTexts`, `save_templates`): o HTML e o CSS do PDF, a prévia da página de
  resultado e as notas que o PDF pode mostrar.
- Salvar confere a definição inteira, como o banco a guarda (`compile_definition` sobre a cópia do
  JSONB): o que não compila não é salvo, com os problemas no caminho ou na linha. Um rascunho
  guardado sempre compila. O editor devolve a impressão digital do rascunho que mostrou
  (`Draft.token`): se o rascunho mudou desde então (outra aba, outra publicação), nada muda
  (`DraftChanged`). O rascunho nunca é servido em público: o registro só conhece as versões.
- `assemble` monta as instruções e o prompt com as respostas de exemplo ou com as que a operadora
  preencheu, e na data simulada: o rascunho compila com o relógio parado nela. `score` calcula a
  pontuação do rascunho com as mesmas respostas: cada nota, a prioridade, a persona e a regra que
  decidiu as duas.
- Voltar a uma versão (`revert`) copia a definição dela para o rascunho.

Teste do rascunho
-----------------
O teste mora em `app.forms.draft_tests`. Um teste com sucesso grava no rascunho o que ia ao LLM
(`FormDraft.tested_fingerprint`): a prova que a publicação pede, que fica mesmo depois de a
retenção apagar o teste.

Publicação
----------
`publish` cria a versão seguinte a partir do rascunho, com a nota, e pede o mesmo que a importação
(`app.forms.stored`): a definição como o banco a guarda compila, a confirmação quando o prompt cita
o nome, o e-mail ou o telefone da lead e o PDF de exemplo no adaptador do PDF. Se mudou algo que vai
ao LLM ou o valida (`app.forms.definition.changes.llm_parts`), pede também um teste com sucesso do
que vai ao LLM agora. A versão fixa o conteúdo de cada imagem que o PDF e a prévia usam
(`app.forms.assets`): trocar uma imagem que a versão atual usa deixa o rascunho pendente, e é a
publicação que a leva para os envios novos. A versão do texto do consentimento sobe sozinha quando
o texto muda. A situação do formulário não muda, e `forms.title` passa a ser o título da versão.
`review` diz, antes, o que a publicação faria e o que falta, sem gerar o PDF. `publish(dry_run=True)`
faz tudo o que a publicação faz, inclusive o PDF de exemplo no adaptador, menos gravar e auditar, e
devolve a `Simulation`: o que falta (`reasons` e `problems`, os mesmos da recusa, completos) em vez
de levantar `PublishRefused`.

Formulário novo
---------------
`create` cria o formulário sem versão e o rascunho dele, a partir do modelo inicial
(`app.forms.starter`) ou da cópia de outro formulário: a versão publicada dele (ou o rascunho, se
ele ainda não foi publicado). O endereço segue o formato e as recusas da importação
(`app.forms.stored.slug_problem`).

Toda mudança fica na auditoria (`form.create`, `form.duplicate`, `form.rename`, `form.draft.save`,
`form.draft.revert`, `form.draft.test` e `form.publish`), com o slug, os números e as impressões
digitais, nunca o conteúdo nem as respostas.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from functools import cached_property
from typing import TYPE_CHECKING, Any, Literal, overload

import sqlalchemy as sa
from pydantic import BaseModel, ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.forms import Completion, FormSpec, GenerationUnavailable, UnknownForm, draft_patch, starter
from app.forms import assets as form_assets
from app.forms.dates import LEAD_TIMEZONE, local_date
from app.forms.definition import (
    Clock,
    DefinitionError,
    Problem,
    compile_definition,
    fingerprint,
    utc_now,
)
from app.forms.definition.changes import (
    DroppedValue,
    SectionChange,
    compare,
    consent_version,
    dropped_option_values,
    llm_changes,
    llm_fingerprint,
)
from app.forms.definition.document import (
    JSON_MESSAGES,
    FormDocument,
    validation_problems,
    value_problems,
)
from app.forms.definition.images import AssetRef
from app.forms.definition.normal import answered, part, same_meaning
from app.forms.definition.resources import opening_logo_problems, resource_problems
from app.forms.definition.scoring import ScoreRun, ScoringFailure
from app.forms.sandbox import TemplateProblem
from app.forms.stored import (
    FIRST_VERSION,
    NOTE_MAX_LENGTH,
    SLUG_IN_DATABASE,
    ContactRole,
    StoredVersion,
    as_stored,
    contact_in_prompt,
    publication_problems,
    slug_of,
    slug_problem,
    stored_version,
)
from app.models import AuditLog, DraftTest, Form, FormDraft, FormVersion, WebhookDestination

if TYPE_CHECKING:
    from app.deps import Deps


# As partes da definição que o editor mostra fora do JSON do resto, cada uma no campo dela.
OWN_FIELDS = ("instructions", "prompt")
# O PDF e a prévia, que a página do PDF edita (`save_templates`).
TEMPLATE_FIELDS = ("pdf", "preview")
# As que o editor estruturado cobre (`app.admin.form_editor`), com as respostas de exemplo.
SCREEN_FIELDS = (
    "generation",
    "title",
    "intro",
    "opening_logo",
    "minutes",
    "steps",
    "identity",
    "consent",
    "plan",
    "phases",
    "data",
    "scoring",
    "notices",
    "completion",
    "release",
)
# O que o resto não traz: o endereço, que só muda antes da primeira publicação e no campo dele, e
# a versão do texto do consentimento, que a publicação sobe sozinha. Salvar põe de volta os
# valores do rascunho.
SLUG_FIXED = "o endereço do formulário não muda depois da primeira publicação; tire o slug do JSON"
SLUG_PUBLISHED = "o endereço do formulário não muda depois da primeira publicação"
SLUG_OF_THE_DOCUMENT = "o endereço do documento tem de ser o do formulário"
NOT_AN_OBJECT_DOCUMENT = "a definição precisa ser um objeto JSON"
OWN_FIELD = "fica no campo próprio do editor; tire do JSON"
TEMPLATE_FIELD = "fica no editor do PDF e da prévia; tire do JSON"
SCREEN_FIELD = "fica no editor estruturado, mais acima; tire do JSON"
NOT_AN_OBJECT = "precisa ser um objeto JSON, entre chaves"
UNKNOWN_ANSWER = "resposta para uma pergunta que o formulário não tem"


class DraftChanged(Exception):
    """O rascunho mudou desde que a página foi aberta (outra aba, um clique repetido, uma
    publicação): nada foi gravado."""


@dataclass(frozen=True)
class Draft:
    """O rascunho de um formulário do banco, como a operadora o edita: o guardado ou, antes da
    primeira mudança, a versão atual. Um formulário criado do zero ou duplicado no admin tem o
    rascunho antes da versão 1: sem versão atual (`current` None), ele é todo pendente."""

    form_id: uuid.UUID
    slug: str
    title: str  # o nome do formulário no admin (`forms.title`)
    current: StoredVersion | None  # a versão publicada atual; None antes da primeira publicação
    document: dict[str, Any]  # a definição do rascunho, como o banco a guarda
    base_version: int | None  # de que versão ele foi copiado; None antes da primeira publicação
    updated_by: str | None  # None: sem rascunho guardado
    updated_at: datetime | None
    # O que ia ao LLM no último teste com sucesso deste rascunho, e quando ele terminou.
    tested_fingerprint: str | None = None
    tested_at: datetime | None = None
    # As imagens que a versão atual fixou e que foram trocadas desde então, entre as que o
    # rascunho usa (`app.forms.assets.stale_names`): há o que publicar mesmo sem mudar o texto.
    stale_assets: tuple[str, ...] = ()

    @property
    def saved(self) -> bool:
        return self.updated_by is not None

    @property
    def token(self) -> str:
        """O que o editor devolve: a impressão digital do rascunho que ele mostrou."""
        return fingerprint(self.document)

    @property
    def published(self) -> bool:
        """O formulário já tem uma versão publicada: o endereço dele não muda mais."""
        return self.current is not None

    @cached_property
    def edited(self) -> bool:
        """O rascunho diz outra coisa que a versão atual. Os padrões do formato escritos por extenso
        (`"required": true`) não contam (`app.forms.definition.normal`): o editor não os escreve, e
        o rascunho só com essa diferença não tem o que publicar."""
        if self.current is None:
            return True
        if not self.saved or self.token == self.current.fingerprint:
            return False
        return not same_meaning(self.document, self.current.definition)

    @property
    def pending(self) -> bool:
        """Há uma mudança que ainda não foi publicada (antes da versão 1, o rascunho inteiro): o
        texto do rascunho mudou, ou uma imagem que ele usa foi trocada."""
        return self.edited or bool(self.stale_assets)

    @property
    def next_number(self) -> int:
        """O número da versão que a publicação cria."""
        return self.current.number + 1 if self.current is not None else FIRST_VERSION


def load(session: Session, slug: str) -> Draft | None:
    """O rascunho do formulário com este slug; None se não há formulário com uma versão publicada
    ou um rascunho guardado."""
    found = session.execute(sa.select(Form.id, Form.title).where(Form.slug == slug)).one_or_none()
    if found is None:
        return None
    form_id, title = found
    current = stored_version(session, slug)
    row = session.get(FormDraft, form_id)
    if row is None:
        if current is None:  # só a linha de situação, sem versão nem rascunho
            return None
        draft = Draft(form_id, slug, title, current, current.definition, current.number, None, None)
    else:
        draft = Draft(
            form_id,
            slug,
            title,
            current,
            row.definition,
            row.base_version,
            row.updated_by,
            row.updated_at,
            row.tested_fingerprint,
            row.tested_at,
        )
    if current is None:
        return draft
    stale = form_assets.stale_names(session, slug, draft.document, current.number)
    return replace(draft, stale_assets=stale)


def pending_slugs(session: Session) -> frozenset[str]:
    """Os formulários com rascunho pendente: uma linha de rascunho que diz outra coisa que a versão
    atual, um rascunho de formulário ainda sem versão publicada ou uma imagem da versão atual que
    foi trocada (`app.forms.assets.stale_slugs`). O banco separa as linhas de impressão digital
    diferente; destas, só ficam as que não dizem o mesmo que a versão (`Draft.edited`)."""
    latest = (
        sa.select(FormVersion.form_id, sa.func.max(FormVersion.number).label("number"))
        .group_by(FormVersion.form_id)
        .subquery()
    )
    statement = (
        sa.select(Form.slug, FormDraft.definition, FormVersion.definition)
        .join(FormDraft, FormDraft.form_id == Form.id)
        .outerjoin(latest, latest.c.form_id == Form.id)
        .outerjoin(
            FormVersion,
            sa.and_(FormVersion.form_id == Form.id, FormVersion.number == latest.c.number),
        )
        .where(
            sa.or_(
                FormVersion.fingerprint.is_(None),
                FormDraft.fingerprint != FormVersion.fingerprint,
            )
        )
    )
    edited = {
        slug
        for slug, draft, published in session.execute(statement)
        if published is None or not same_meaning(draft, published)
    }
    return frozenset(edited) | form_assets.stale_slugs(session)


# --- o editor ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftTexts:
    """O rascunho como o editor o mostra e recebe: as instruções e o prompt em texto, o resto da
    definição em JSON e, do editor estruturado (`app.admin.form_editor`), a página, as etapas, a
    identidade e o consentimento (`screen`, já no formato da definição) e as respostas de
    exemplo."""

    instructions: str
    prompt: str
    definition: str
    screen: Mapping[str, Any]
    example_answers: Mapping[str, Any]
    # O que o editor estruturado não conseguiu escrever na definição (um bloco dos dados sem
    # nome, por exemplo): salvar para antes de compilar, com estes problemas.
    problems: tuple[Problem, ...] = ()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def texts_of(document: Mapping[str, Any]) -> DraftTexts:
    """Os campos do editor para uma definição. O resto sai na ordem do formato (a do documento,
    para as chaves que o formato não conhece), sem o slug e sem o que tem editor próprio."""
    order = list(FormDocument.model_fields)
    rest = {
        key: document[key]
        for key in sorted(
            document, key=lambda key: order.index(key) if key in order else len(order)
        )
        if key not in (*OWN_FIELDS, *SCREEN_FIELDS, *TEMPLATE_FIELDS, "slug")
    }
    examples = rest.get("examples")
    answers: Any = {}
    if isinstance(examples, Mapping):
        answers = examples.get("answers", {})
        rest["examples"] = {k: v for k, v in examples.items() if k != "answers"}
    screen = {key: document[key] for key in SCREEN_FIELDS if key in document}
    if isinstance(screen.get("consent"), Mapping):
        screen["consent"] = {k: v for k, v in screen["consent"].items() if k != "version"}
    return DraftTexts(
        instructions=str(document.get("instructions", "")),
        prompt=str(document.get("prompt", "")),
        definition=_json(rest),
        screen=screen,
        example_answers=answers if isinstance(answers, Mapping) else {},
    )


def _lines(text: str) -> str:
    """O texto como o editor o guardou: o navegador manda as quebras de linha como CRLF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse_json(text: str, path: str) -> tuple[Any, list[Problem]]:
    try:
        return json.loads(text), []
    except json.JSONDecodeError as exc:
        detail = JSON_MESSAGES.get(exc.msg, "formato inválido")
        message = f"JSON inválido: {detail} (coluna {exc.colno})"
        return None, [Problem(path, message, exc.lineno)]


def _keep_wording(document: dict[str, Any], previous: Mapping[str, Any]) -> None:
    """O editor estruturado escreve só o que difere do padrão do formato, e uma definição
    importada pode trazer o padrão por extenso (`"required": true`). A parte que diz o mesmo que
    antes volta como estava escrita: salvar sem mudança não cria rascunho pendente, e a
    comparação entre versões não mostra o que não mudou."""
    for key in SCREEN_FIELDS:
        if key not in previous or key not in document:
            continue
        before, after = part(key, previous[key]), part(key, document[key])
        if before is not None and before == after:
            document[key] = previous[key]
    before_examples, examples = previous.get("examples"), document.get("examples")
    if isinstance(before_examples, Mapping) and isinstance(examples, dict):
        answers = before_examples.get("answers")
        if answered(answers) == answered(examples.get("answers")):
            examples["answers"] = answers
    if "data" not in previous and document.get("data") == {}:
        del document["data"]  # sem blocos, o editor não escreve a chave que o formato dispensa


def document_of(
    texts: DraftTexts, previous: Mapping[str, Any], *, slug: str | None = None
) -> dict[str, Any]:
    """A definição que o editor mandou, com o slug (o do rascunho anterior, ou `slug`) e a versão
    do consentimento do rascunho anterior (ou a primeira do slug novo). Levanta `DefinitionError`
    se o JSON não lê, se o resto traz o que tem editor próprio ou se algo fixo foi mexido."""
    if texts.problems:
        raise DefinitionError(texts.problems)
    rest, problems = _parse_json(_lines(texts.definition), "")
    if problems:
        raise DefinitionError(problems)
    if not isinstance(rest, dict):
        raise DefinitionError([Problem("", f"o resto da definição {NOT_AN_OBJECT}")])
    problems = [Problem(key, OWN_FIELD) for key in OWN_FIELDS if key in rest]
    problems += [Problem(key, SCREEN_FIELD) for key in SCREEN_FIELDS if key in rest]
    problems += [Problem(key, TEMPLATE_FIELD) for key in TEMPLATE_FIELDS if key in rest]
    if "slug" in rest:
        problems.append(Problem("slug", SLUG_FIXED))
    examples = rest.get("examples")
    if isinstance(examples, dict) and "answers" in examples:
        problems.append(Problem("examples.answers", SCREEN_FIELD))
    if problems:
        raise DefinitionError(problems)
    document: dict[str, Any] = {
        **rest,
        **texts.screen,
        "slug": slug or previous["slug"],
        "instructions": _lines(texts.instructions),
        "prompt": _lines(texts.prompt),
        **{key: previous[key] for key in TEMPLATE_FIELDS if key in previous},
    }
    consent = texts.screen.get("consent")
    before = previous.get("consent")
    if isinstance(consent, Mapping):
        version = f"{slug}-v1" if slug else None
        if version is None and isinstance(before, Mapping):
            version = before.get("version")
        document["consent"] = {**consent, "version": version}
    answers = dict(texts.example_answers)
    if isinstance(examples, dict):
        document["examples"] = {**examples, "answers": answers}
    elif examples is None:
        document["examples"] = {"answers": answers}
    if document.get("generation", "pdf") == "none":
        for key in ("instructions", "prompt", "plan", "pdf", "preview", "phases"):
            document.pop(key, None)
        if isinstance(document.get("examples"), dict):
            document["examples"].pop("plan", None)
    elif previous.get("generation") == "none" and document.get("generation", "pdf") == "pdf":
        # A operadora habilitou geração explicitamente: começa pelo mesmo modelo PDF da criação.
        # Nenhum bloco de geração fica guardado enquanto o rascunho é captura.
        _prepare_pdf(document)
    _keep_wording(document, previous)
    # O que o JSONB não guarda (NaN, o caractere nulo) sai aqui, com o caminho: no banco, viraria
    # um erro com um trecho da definição na mensagem.
    problems = value_problems(document)
    if problems:
        raise DefinitionError(problems)
    return document


def _prepare_pdf(document: dict[str, Any]) -> None:
    """Prepara geração num rascunho que era captura, preservando perguntas e respostas."""
    initial = starter.definition(slug=document["slug"], title=str(document.get("title", "")))
    for key in ("instructions", "plan", "pdf", "preview"):
        document[key] = initial[key]
    document["prompt"] = starter.prompt_of(document.get("steps", ()), document.get("identity", {}))
    document["examples"] = {**document["examples"], "plan": initial["examples"]["plan"]}


# --- o prompt montado -------------------------------------------------------------------------

EARLIEST_DAY = date(2000, 1, 1)
LATEST_DAY = date(2100, 12, 31)
INVALID_DATE = "use uma data válida, como 2027-03-01"
DATE_OUT_OF_RANGE = "use uma data entre 2000 e 2100"
NOON = time(12, 0)  # a data simulada vale o dia inteiro em São Paulo


@dataclass(frozen=True)
class Assembled:
    """O que vai ao LLM com estas respostas, nesta data: as instruções e o prompt montado, com o
    contrato compilado do rascunho e as respostas já validadas por ele."""

    spec: FormSpec
    answers: BaseModel
    answers_text: str  # as respostas como o campo mostra, em JSON
    instructions: str
    prompt: str
    today: date  # a data de hoje que o prompt recebeu
    simulated: date | None
    examples: bool  # as respostas de exemplo do rascunho


def _simulated(text: str) -> date | None:
    """A data simulada do campo (AAAA-MM-DD, como o navegador manda); None se veio vazia."""
    if not text.strip():
        return None
    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        raise DefinitionError([Problem("date", INVALID_DATE)]) from None
    if not EARLIEST_DAY <= day <= LATEST_DAY:
        raise DefinitionError([Problem("date", DATE_OUT_OF_RANGE)])
    return day


def simulated_moment(day: date | None, now: datetime) -> datetime:
    """O momento que o rascunho vê: o meio-dia da data simulada em São Paulo, ou `now`."""
    return datetime.combine(day, NOON, tzinfo=LEAD_TIMEZONE) if day else now


def _validated(
    spec: FormSpec, draft: Draft, answers: str | Mapping[str, Any] | None
) -> tuple[BaseModel, str]:
    """As respostas de exemplo do rascunho (`answers` None) ou as que a operadora preencheu,
    validadas pelas perguntas dele; e o texto como o campo as mostra. Elas chegam em JSON, como o
    campo da página as manda (texto), ou como um objeto, como a API de admin as manda: os dois caem
    na mesma conferência. Levanta `DefinitionError` com os problemas, sem repetir os valores.

    Uma resposta para uma pergunta que o formulário não tem aponta a chave no texto da página
    (`answers.<chave>`: é o que a operadora escreveu na tela dela). No objeto da API a chave é texto
    de quem chama, e o contrato não devolve o que recebeu: o problema é um só, em `answers`, sem a
    chave."""
    unknown_answer = False
    if answers is None:
        given: Any = draft.document["examples"]["answers"]
        shown = _json(given)
    elif isinstance(answers, str):
        shown = _lines(answers)
        given, problems = _parse_json(shown, "answers")
        if problems:
            raise DefinitionError(problems)
        if not isinstance(given, dict):
            raise DefinitionError([Problem("answers", f"as respostas {NOT_AN_OBJECT}")])
        problems = value_problems(given, ("answers",))  # o teste guarda as respostas num JSONB
        if problems:
            raise DefinitionError(problems)
    else:
        given = dict(answers)
        shown = _json(given)
        problems = value_problems(given, ("answers",))  # o mesmo que o texto: o JSONB do teste
        if problems:
            raise DefinitionError(problems)
        known = spec.answers_model.model_fields
        unknown_answer = any(key not in known for key in given)
        if unknown_answer:  # o resto é conferido, e os problemas saem juntos
            given = {key: value for key, value in given.items() if key in known}
    validated: BaseModel | None = None
    problems = []
    try:
        validated = spec.answers_model.model_validate(given)
    except ValidationError as exc:
        problems = validation_problems(exc, "answers")
    if unknown_answer:
        problems.append(Problem("answers", UNKNOWN_ANSWER))
    if problems or validated is None:
        raise DefinitionError(problems)
    return validated, shown


def assemble(
    draft: Draft,
    *,
    answers: str | Mapping[str, Any] | None = None,
    day: str = "",
    now: datetime,
) -> Assembled:
    """O prompt montado do rascunho com as respostas de exemplo (`answers` None) ou com as que a
    operadora preencheu (em JSON, como texto ou como objeto), e na data simulada (`day`) ou na de
    hoje (`now`). O rascunho é compilado com o relógio parado nessa data: o prompt a recebe em
    `today`. Levanta `DefinitionError` com os problemas das respostas, da data ou da montagem, sem
    repetir os valores."""
    simulated = _simulated(day)
    moment = simulated_moment(simulated, now)
    spec = compile_definition(
        draft.document, version_number=draft.next_number, clock=lambda: moment
    )
    spec.require_generation()
    validated, shown = _validated(spec, draft, answers)
    try:
        prompt = spec.render_prompt(validated)
    except TemplateProblem as exc:
        raise DefinitionError([Problem(exc.name, exc.detail, exc.line)]) from None
    return Assembled(
        spec=spec,
        answers=validated,
        answers_text=shown,
        instructions=spec.instructions,
        prompt=prompt,
        today=local_date(moment),
        simulated=simulated,
        examples=answers is None,
    )


@dataclass(frozen=True)
class Scored:
    """A pontuação do rascunho com estas respostas. `run` é None se ele não tem pontuação."""

    spec: FormSpec
    answers_text: str  # as respostas como o campo mostra, em JSON
    run: ScoreRun | None
    examples: bool  # as respostas de exemplo do rascunho


def score(draft: Draft, *, answers: str | Mapping[str, Any] | None = None, now: datetime) -> Scored:
    """A pontuação do rascunho com as respostas de exemplo (`answers` None) ou com as que a
    operadora preencheu (em JSON, como texto ou como objeto): cada nota, a prioridade, a persona e
    a regra que decidiu as duas. Não grava nada. Levanta `DefinitionError` com os problemas das
    respostas ou, se uma fórmula não calcula com elas (divisão por zero), com o caminho da
    fórmula."""
    spec = compile_definition(draft.document, version_number=draft.next_number, clock=lambda: now)
    validated, shown = _validated(spec, draft, answers)
    run = None
    if spec.scoring is not None:
        try:
            run = spec.scoring.run(validated)
        except ScoringFailure as failure:
            raise DefinitionError(
                [Problem(failure.path, f"com estas respostas, {failure.detail}")]
            ) from None
    return Scored(spec=spec, answers_text=shown, run=run, examples=answers is None)


# --- o PDF e a prévia ------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateTexts:
    """O PDF e a prévia do rascunho como o editor do PDF os mostra e recebe: o HTML e o CSS do PDF,
    a prévia da página de resultado e as notas que o PDF e a prévia podem mostrar."""

    pdf_html: str
    pdf_css: str
    preview: str
    scores: tuple[str, ...]


def template_texts_of(document: Mapping[str, Any]) -> TemplateTexts:
    """Os campos do editor do PDF para uma definição."""
    pdf = document.get("pdf")
    pdf = pdf if isinstance(pdf, Mapping) else {}
    scores = pdf.get("scores")
    return TemplateTexts(
        pdf_html=str(pdf.get("html", "")),
        pdf_css=str(pdf.get("css", "")),
        preview=str(document.get("preview", "")),
        scores=tuple(str(name) for name in scores) if isinstance(scores, list) else (),
    )


def templates_document(texts: TemplateTexts, previous: Mapping[str, Any]) -> dict[str, Any]:
    """A definição `previous` com o PDF e a prévia que o editor do PDF mandou. O CSS e as notas só
    entram no documento se têm conteúdo ou se o documento já os trazia: salvar sem mudança não cria
    rascunho pendente."""
    before = previous.get("pdf")
    before = before if isinstance(before, Mapping) else {}
    pdf: dict[str, Any] = {**before, "html": _lines(texts.pdf_html)}
    for key, value in (("css", _lines(texts.pdf_css)), ("scores", list(texts.scores))):
        if value or key in before:
            pdf[key] = value
    return {**previous, "pdf": pdf, "preview": _lines(texts.preview)}


def check_templates(
    texts: TemplateTexts,
    document: dict[str, Any],
    *,
    number: int,
    clock: Clock = utc_now,
    assets: Mapping[str, AssetRef] | None = None,
) -> FormSpec:
    """O contrato do rascunho `document` com este PDF e esta prévia. Levanta `DefinitionError` com
    tudo o que impede o PDF e a prévia de valer: o que não compila (com a linha do template), o
    que o PDF carregaria de fora do app e a imagem que ele pede e não existe. `assets` são as
    imagens que o formulário enxerga, pelo nome (`app.forms.assets.visible`); o contrato sai com as
    que os templates usam, no conteúdo de agora."""
    known = assets or {}
    problems: list[Problem] = []
    spec: FormSpec | None = None
    try:
        spec = compile_definition(document, version_number=number, clock=clock)
    except DefinitionError as exc:
        problems = list(exc.problems)
    problems += resource_problems(
        _lines(texts.pdf_html),
        _lines(texts.pdf_css),
        preview=_lines(texts.preview),
        assets=known.keys(),
    )
    if problems or spec is None:
        raise DefinitionError(problems)
    used = {name: known[name] for name in form_assets.used_names(document) if name in known}
    return spec.with_assets(used)


def _rename_of(session: Session, draft: Draft, new_slug: str | None) -> str | None:
    """O endereço novo do formulário, se `new_slug` pede outro; None se ele fica. Antes da primeira
    publicação o endereço muda (e tem de servir: `slug_problem`); depois dela, não. Levanta
    `DefinitionError` (campo `slug`) se o endereço novo não serve."""
    new_slug = (new_slug or "").strip() or None
    rename = new_slug if new_slug is not None and new_slug != draft.slug else None
    if rename is not None:
        from app.models import HandoffReceiver

        if session.scalar(
            sa.select(HandoffReceiver.id).where(HandoffReceiver.form_slug == draft.slug)
        ):
            raise DefinitionError(
                [
                    Problem(
                        "slug",
                        "este endereço já tem receptor de encaminhamento; crie uma cópia para usar outro endereço",
                    )
                ]
            )
        refused = SLUG_PUBLISHED if draft.published else slug_problem(session, rename)
        if refused is not None:
            raise DefinitionError([Problem("slug", refused)])
    return rename


def _with_consent_version(
    document: Mapping[str, Any], previous: Mapping[str, Any], rename: str | None
) -> dict[str, Any]:
    """O documento com a versão do texto do consentimento que o sistema dá: a do rascunho anterior
    ou, no endereço novo, a primeira dele (`<slug>-v1`). Quem edita o texto não escolhe a versão: a
    publicação a sobe sozinha quando o texto muda (`changes.consent_version`), e é essa a versão que
    cada envio grava ao lado do texto que a lead leu. O editor das perguntas já a mantém (`document_of`);
    um documento que chega inteiro, pela API, também."""
    consent = document.get("consent")
    if not isinstance(consent, Mapping):  # a compilação aponta o que falta
        return dict(document)
    before = previous.get("consent")
    if rename is not None:
        version = f"{rename}-v1"
    else:
        version = before.get("version") if isinstance(before, Mapping) else None
    return {**document, "consent": {**consent, "version": version}}


def save_document(
    deps: Deps,
    slug: str,
    document: dict[str, Any],
    *,
    actor: str,
    expected: str,
    new_slug: str | None = None,
    resources: bool = True,
) -> Draft:
    """Guarda `document` como o rascunho do formulário `slug`, em nome de `actor`, e devolve o rascunho
    guardado. É o miolo comum dos salvares: o do editor (`save`), o do PDF (`save_templates`) e o da
    API, que troca o documento inteiro (`replace_document`) ou o remenda (`patch_document`).
    `expected` é o `Draft.token` que quem edita viu. Antes da primeira publicação, `new_slug` muda o
    endereço do formulário junto (o rascunho, o formulário e o destino do webhook dele); depois
    dela, o endereço não muda mais. O `slug` do documento tem de ser o do formulário, ou o
    `new_slug`. A versão do texto do consentimento é do sistema (`_with_consent_version`).

    Confere o documento inteiro, como o banco o guarda: o que o JSONB não guarda (NaN, o caractere nulo),
    a compilação e, com `resources` (o padrão), o que o PDF e a prévia pedem (só arquivos de
    `app/assets`, imagens que existem e `data:`). O salvar do editor das perguntas não mexe no PDF e na
    prévia, e por isso passa `resources=False`: o PDF que o rascunho já tinha não passa a barrá-lo.

    Levanta `UnknownForm`, `DraftChanged` ou `DefinitionError`; nos dois últimos, nada é gravado."""
    with deps.sessions() as session:
        draft = load(session, slug)
        if draft is None:
            raise UnknownForm(slug)
        if draft.token != expected:
            raise DraftChanged
        rename = _rename_of(session, draft, new_slug)
        if not isinstance(document, Mapping):
            raise DefinitionError([Problem("", NOT_AN_OBJECT_DOCUMENT)])
        # O que o JSONB não guarda sai antes, com o caminho: no banco, viraria um erro com um trecho da
        # definição na mensagem.
        problems = value_problems(document)
        if problems:
            raise DefinitionError(problems)
        wanted = rename or slug
        if isinstance(document.get("slug"), str) and document["slug"] != wanted:
            raise DefinitionError([Problem("slug", SLUG_OF_THE_DOCUMENT)])
        stored = as_stored(session, _with_consent_version(document, draft.document, rename))
        library = {n: a.ref for n, a in form_assets.visible(session, slug).items()}
    if problems := opening_logo_problems(stored, library.keys()):
        raise DefinitionError(problems)
    if resources and stored.get("generation", "pdf") == "pdf":
        check_templates(
            template_texts_of(stored), stored, number=draft.next_number, assets=library
        )  # DefinitionError
    else:
        compile_definition(stored, version_number=draft.next_number)  # DefinitionError
    _store(deps, draft, stored, actor=actor, expected=expected, rename=rename)
    with deps.sessions() as session:
        saved = load(session, wanted)
    assert saved is not None
    return saved


def save_templates(
    deps: Deps, slug: str, texts: TemplateTexts, *, actor: str, expected: str
) -> Draft:
    """Guarda o PDF e a prévia do rascunho que o editor do PDF mandou, em nome de `actor`, e
    devolve o rascunho guardado. `expected` é o `Draft.token` que a página mostrou. Como o salvar
    do editor, confere a definição inteira, como o banco a guarda, e mais: o PDF só pede arquivos
    de `app/assets`, imagens que existem e `data:` (`save_document`). Levanta `UnknownForm`,
    `DraftChanged` ou `DefinitionError`; nos dois últimos, nada é gravado."""
    with deps.sessions() as session:
        draft = load(session, slug)
        if draft is None:
            raise UnknownForm(slug)
        if draft.token != expected:
            raise DraftChanged
    compile_definition(draft.document).require_generation()
    document = templates_document(texts, draft.document)
    return save_document(deps, slug, document, actor=actor, expected=expected)


def save(
    deps: Deps,
    slug: str,
    texts: DraftTexts,
    *,
    actor: str,
    expected: str,
    new_slug: str | None = None,
) -> Draft:
    """Guarda o rascunho que o editor mandou, em nome de `actor`, e devolve o rascunho guardado.
    `expected` é o `Draft.token` que o editor mostrou. Antes da primeira publicação, `new_slug`
    muda o endereço do formulário junto (o rascunho, o formulário e o destino do webhook dele);
    depois dela, o endereço não muda mais. Levanta `UnknownForm`, `DraftChanged` ou
    `DefinitionError`; nos dois últimos, nada é gravado."""
    with deps.sessions() as session:
        draft = load(session, slug)
        if draft is None:
            raise UnknownForm(slug)
        if draft.token != expected:
            raise DraftChanged
        rename = _rename_of(session, draft, new_slug)
    document = document_of(texts, draft.document, slug=rename)
    return save_document(
        deps,
        slug,
        document,
        actor=actor,
        expected=expected,
        new_slug=rename,
        resources=False,  # o editor das perguntas não mexe no PDF nem na prévia
    )


def _address_of(document: Any) -> str | None:
    """O endereço que um documento inteiro diz ter, se ele diz um texto: é o pedido de mudar o
    endereço do formulário (só antes da primeira publicação, `save_document`)."""
    address = document.get("slug") if isinstance(document, Mapping) else None
    return address if isinstance(address, str) else None


def replace_document(
    deps: Deps, slug: str, document: dict[str, Any], *, actor: str, expected: str
) -> Draft:
    """Troca o rascunho do formulário pelo documento inteiro (a API de admin: `PUT`), em nome de
    `actor`, e devolve o rascunho guardado. O endereço do documento é o do formulário; antes da
    primeira publicação, um endereço novo muda o do formulário junto. Faz as conferências dos dois
    salvares da página: a compilação e o que o PDF e a prévia pedem (`save_document`). Levanta
    `UnknownForm`, `DraftChanged` ou `DefinitionError`; nos dois últimos, nada é gravado."""
    return save_document(
        deps, slug, document, actor=actor, expected=expected, new_slug=_address_of(document)
    )


def patch_document(
    deps: Deps,
    slug: str,
    operations: Sequence[Mapping[str, Any]],
    *,
    actor: str,
    expected: str,
) -> Draft:
    """Edita o rascunho do formulário por um JSON Patch (RFC 6902; a API de admin: `PATCH`), em nome
    de `actor`, e devolve o rascunho guardado. O patch é aplicado numa cópia do documento
    (`app.forms.draft_patch`) e o resultado é conferido por inteiro, como o de `replace_document`,
    na mesma transação: **tudo ou nada**. Uma operação que não se aplica é o problema `patch.<n>`.
    Levanta `UnknownForm`, `DraftChanged` ou `DefinitionError`; nos dois últimos, nada é
    gravado."""
    with deps.sessions() as session:
        draft = load(session, slug)
        if draft is None:
            raise UnknownForm(slug)
        if draft.token != expected:
            raise DraftChanged
    patched = draft_patch.apply(draft.document, operations)  # DefinitionError
    return save_document(
        deps, slug, patched, actor=actor, expected=expected, new_slug=_address_of(patched)
    )


def revert(deps: Deps, slug: str, number: int, *, actor: str, expected: str) -> Draft:
    """Copia a versão `number` para o rascunho, em nome de `actor`, no lugar do que ele tinha. Ela
    segue o caminho normal: teste, se o que vai ao LLM difere da versão atual, e publicação, que
    cria uma versão nova igual à antiga. Voltar à versão atual descarta o rascunho. Levanta
    `UnknownForm` (o formulário ou a versão não existe), `DraftChanged` ou `DefinitionError` (a
    versão não compila mais com este deploy)."""
    with deps.sessions() as session:
        draft = load(session, slug)
        version = stored_version(session, slug, number) if draft is not None else None
    if draft is None or version is None:
        raise UnknownForm(slug, number)
    if draft.token != expected:
        raise DraftChanged
    compile_definition(version.definition, version_number=draft.next_number)
    _store(
        deps,
        draft,
        version.definition,
        actor=actor,
        expected=expected,
        reverted_to=number,
        action="form.draft.revert",
        details={"version": number},
    )
    with deps.sessions() as session:
        reverted = load(session, slug)
    assert reverted is not None
    return reverted


def _store(
    deps: Deps,
    draft: Draft,
    document: dict[str, Any],
    *,
    actor: str,
    expected: str,
    reverted_to: int | None = None,
    action: str = "form.draft.save",
    details: Mapping[str, Any] | None = None,
    rename: str | None = None,
) -> None:
    """Grava o rascunho, se ele ainda é o que a página mostrou, e a auditoria. Igual à versão
    atual, o rascunho sai: não há o que publicar. Com `rename`, o formulário (sem versão
    publicada) muda de endereço na mesma transação, e o destino do webhook dele vai junto.

    Publicar o rascunho que a página mostrou pode deixar o token dela igual (a versão nova é o
    próprio rascunho), por isso o que depende da versão atual sai do formulário lido sob o
    bloqueio: se o endereço ainda muda e de que versão o rascunho veio (com `reverted_to`, a
    escolhida)."""
    digest = fingerprint(document)
    now = utc_now()
    try:
        with deps.sessions.begin() as session:
            from app.forms.continuation_contract import assert_definition_contract
            from app.integration_readiness import configuration_lock

            configuration_lock(session, shared=True)
            assert_definition_contract(session, document)
            # A linha do formulário serializa as mudanças do rascunho e a publicação.
            session.execute(sa.select(Form.id).where(Form.id == draft.form_id).with_for_update())
            latest = load(session, draft.slug)
            if latest is None or latest.token != expected:
                raise DraftChanged
            if rename is not None:
                _rename(session, latest, rename, actor=actor)
            base_version = reverted_to if reverted_to is not None else latest.base_version
            _write_draft(session, latest, document, digest, now, base_version, actor)
            session.add(
                AuditLog(
                    actor=actor,
                    action=action,
                    target_type="form",
                    target_id=str(draft.form_id),
                    details={
                        "form_slug": rename or draft.slug,
                        "fingerprint": digest,
                        **(details or {}),
                    },
                )
            )
    except IntegrityError:  # outro formulário pegou o endereço novo no meio do caminho
        raise DefinitionError([Problem("slug", SLUG_IN_DATABASE)]) from None


def _rename(session: Session, draft: Draft, slug: str, *, actor: str) -> None:
    """O formulário sem versão publicada passa a ter outro endereço, e o destino do webhook dele
    vai junto. Ninguém recebeu o endereço antigo: o formulário nunca esteve no ar."""
    if draft.published:
        raise DefinitionError([Problem("slug", SLUG_PUBLISHED)])
    from app.models import HandoffReceiver

    if session.scalar(sa.select(HandoffReceiver.id).where(HandoffReceiver.form_slug == draft.slug)):
        raise DefinitionError(
            [
                Problem(
                    "slug",
                    "este endereço já tem receptor de encaminhamento; crie uma cópia para usar outro endereço",
                )
            ]
        )
    session.execute(sa.update(Form).where(Form.id == draft.form_id).values(slug=slug))
    session.execute(
        sa.update(WebhookDestination)
        .where(WebhookDestination.form_slug == draft.slug)
        .values(form_slug=slug)
    )
    session.add(
        AuditLog(
            actor=actor,
            action="form.rename",
            target_type="form",
            target_id=str(draft.form_id),
            details={"form_slug": slug, "previous_slug": draft.slug},
        )
    )


def _write_draft(
    session: Session,
    latest: Draft,
    document: dict[str, Any],
    digest: str,
    now: datetime,
    base_version: int | None,
    actor: str,
) -> None:
    """A linha do rascunho: sai se ele volta a dizer o mesmo que a versão atual (ainda que escrito
    de outro jeito, sem os padrões por extenso da importação); senão, é gravada."""
    current = latest.current
    if current is not None and (
        digest == current.fingerprint or same_meaning(document, current.definition)
    ):
        session.execute(sa.delete(FormDraft).where(FormDraft.form_id == latest.form_id))
        return
    if latest.current is None:  # antes da versão 1, o nome no admin segue o rascunho
        session.execute(
            sa.update(Form).where(Form.id == latest.form_id).values(title=document["title"])
        )
    values = {
        "definition": document,
        "fingerprint": digest,
        "base_version": base_version,
        "updated_by": actor,
        "updated_at": now,
    }
    session.execute(
        insert(FormDraft)
        .values(form_id=latest.form_id, **values)
        .on_conflict_do_update(index_elements=[FormDraft.form_id], set_=values)
    )


# --- um formulário novo -----------------------------------------------------------------------

TITLE_REQUIRED = "dê um título ao formulário"
TITLE_TOO_LONG = "use no máximo {length} caracteres"
TITLE_MAX_LENGTH = 300  # o do título na definição
SOURCE_UNKNOWN = "formulário de origem não encontrado; escolha outro"
SOURCE_BROKEN = (
    "a definição do formulário de origem não compila com o código deste deploy ({count}); "
    "escolha outro ou comece do modelo inicial"
)


@dataclass(frozen=True)
class _Origin:
    """A definição de onde a cópia parte e a versão dela: o número da versão publicada, ou None
    para o rascunho de um formulário que ainda não foi publicado. `images` resolve os nomes
    segundo essa mesma escolha: pins publicados ou conteúdo atual do rascunho."""

    document: dict[str, Any]
    version: int | None
    images: Mapping[str, AssetRef]


def _origin(session: Session, source: str, *, slug: str, title: str) -> _Origin | None:
    """A cópia do formulário `source` com o endereço e o título novos; None se ele não existe. A
    versão publicada atual dele (ou o rascunho, se ainda não há versão)."""
    published = stored_version(session, source)
    version: int | None = None
    if published is not None:
        base, version = published.definition, published.number
    else:
        draft = load(session, source)
        if draft is None:
            return None
        base = draft.document
    document = {**base, "slug": slug, "title": title}
    if isinstance(document.get("completion"), Mapping) and document["completion"].get("handoff"):
        document["completion"] = {
            key: value for key, value in document["completion"].items() if key != "handoff"
        }
    if isinstance(document.get("consent"), Mapping):
        document["consent"] = {**document["consent"], "version": f"{slug}-v1"}
    images = (
        form_assets.version_refs(session, source, version)
        if version is not None
        else form_assets.live_refs(session, base)
    )
    return _Origin(document, version, images)


def problems_by_field(problems: Sequence[Problem]) -> dict[str, str]:
    """O motivo de cada campo que `create` recusou (`title`, `slug`, `source`): o primeiro problema de
    cada campo vale. É o que a página de criar mostra ao lado do campo e o que a API devolve em
    `errors`."""
    return {problem.path: problem.message for problem in reversed(problems)}


def create(
    deps: Deps,
    *,
    title: str,
    slug: str,
    actor: str,
    source: str = "",
    generation: str | None = None,
) -> str:
    """Cria um formulário do banco sem versão publicada, em nome de `actor`, e devolve o slug. O
    rascunho parte do modelo inicial (`app.forms.starter`) ou, com `source`, da cópia da definição
    desse formulário (`_origin`), com o título e o endereço novos. Um slug em branco sai do título.
    O formulário só vai ao ar na primeira publicação, que cria a versão 1. Levanta
    `DefinitionError` com os problemas no título (`title`), no endereço (`slug`) ou na origem
    (`source`); nada é gravado nesse caso."""
    if generation not in (None, "pdf", "none"):
        raise DefinitionError([Problem("generation", "use pdf ou none")])
    title, source = title.strip(), source.strip()
    slug = slug.strip() or slug_of(title)
    problems = []
    if not title:
        problems.append(Problem("title", TITLE_REQUIRED))
    elif len(title) > TITLE_MAX_LENGTH:
        problems.append(Problem("title", TITLE_TOO_LONG.format(length=TITLE_MAX_LENGTH)))
    with deps.sessions() as session:
        taken = slug_problem(session, slug)
        if taken is not None:
            problems.append(Problem("slug", taken))
        origin = _origin(session, source, slug=slug, title=title) if source else None
        if source and origin is None:
            problems.append(Problem("source", SOURCE_UNKNOWN))
        if problems:
            raise DefinitionError(problems)
        start = (
            origin.document
            if origin
            else starter.definition(slug=slug, title=title, generation=generation or "pdf")
        )
        if origin and generation == "none":
            start = {
                k: v
                for k, v in start.items()
                if k not in ("instructions", "prompt", "plan", "pdf", "preview", "phases")
            }
            start["generation"] = "none"
            start["examples"] = {"answers": start["examples"]["answers"]}
            if start.get("completion", {}).get("mode", "result") == "result":
                start["completion"] = {"mode": "confirmation"}
        elif origin and generation == "pdf":
            start = {**start, "generation": "pdf"}
            if origin.document.get("generation") == "none":
                _prepare_pdf(start)
        document = as_stored(session, start)
    try:
        spec = compile_definition(document, version_number=FIRST_VERSION)
    except DefinitionError as exc:  # o modelo inicial sempre compila; uma origem pode não compilar
        count = f"{len(exc.problems)} {'problema' if len(exc.problems) == 1 else 'problemas'}"
        raise DefinitionError([Problem("source", SOURCE_BROKEN.format(count=count))]) from None
    if origin is None:
        _insert_form(deps, document, actor=actor, action="form.create", details={})
    else:
        details = {"source": source, "source_version": origin.version}
        images = {}
        if spec.screen and spec.screen.opening_logo:
            name = spec.screen.opening_logo.src[6:]
            if name not in origin.images:
                raise DefinitionError(
                    [
                        Problem(
                            "source", "a imagem do logo de abertura da origem não está disponível"
                        )
                    ]
                )
            images[name] = origin.images[name]
            details["images"] = {name: origin.images[name].sha256}
        _insert_form(
            deps, document, actor=actor, action="form.duplicate", details=details, images=images
        )
    return slug


def _insert_form(
    deps: Deps,
    document: dict[str, Any],
    *,
    actor: str,
    action: str,
    details: Mapping[str, Any],
    images: Mapping[str, AssetRef] | None = None,
) -> None:
    """Grava o formulário novo, sem versão, o rascunho dele e a auditoria, numa transação só."""
    digest = fingerprint(document)
    now = utc_now()
    with deps.sessions.begin() as session:
        from app.forms.continuation_contract import assert_definition_contract

        assert_definition_contract(session, document)
        form_id = session.scalar(
            insert(Form)
            .values(
                id=uuid.uuid4(),
                slug=document["slug"],
                title=document["title"],
                created_by=actor,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[Form.slug])
            .returning(Form.id)
        )
        if form_id is None:  # outro formulário com o mesmo slug passou na frente
            raise DefinitionError([Problem("slug", SLUG_IN_DATABASE)])
        for name, ref in (images or {}).items():
            try:
                form_assets.copy_image(
                    deps, session, form_id=form_id, name=name, ref=ref, actor=actor
                )
            except form_assets.AssetRefused as exc:
                raise DefinitionError([Problem("source", exc.message)]) from None
        session.add(
            FormDraft(
                form_id=form_id,
                definition=document,
                fingerprint=digest,
                base_version=None,
                updated_by=actor,
                updated_at=now,
            )
        )
        session.add(
            AuditLog(
                actor=actor,
                action=action,
                target_type="form",
                target_id=str(form_id),
                details={"form_slug": document["slug"], "fingerprint": digest, **details},
            )
        )


# --- a publicação -----------------------------------------------------------------------------

NOTE_REQUIRED = "Escreva uma nota do que mudou: ela fica no histórico, com a versão."
NOTHING_TO_PUBLISH = "Não há o que publicar: o rascunho está igual à versão publicada."
TEST_REQUIRED = "Teste o rascunho com o LLM antes de publicar: {changes}."
FIRST_TEST_REQUIRED = (
    "Teste o rascunho com o LLM antes da primeira publicação: tudo o que vai ao LLM é novo."
)
CONTACT_UNCONFIRMED = (
    "Confirme que o prompt precisa dos dados de contato da lead que ele manda ao LLM."
)
DOES_NOT_COMPILE = "Corrija o rascunho: ele não compila com o código deste deploy."
PDF_FAILED = "Corrija o rascunho: o PDF de exemplo não foi gerado."
OPENING_LOGO_MISSING = "Confira a imagem do logo de abertura antes de publicar."
IMAGE_GONE = (
    "Uma imagem que o PDF ou a prévia usa saiu enquanto você publicava. Confira as imagens e "
    "tente de novo."
)

# O que a publicação pede do teste do que vai ao LLM (`Review.test_status`).
LlmTestStatus = Literal["not_needed", "covered", "missing"]
TEST_NOT_NEEDED: LlmTestStatus = "not_needed"
TEST_COVERED: LlmTestStatus = "covered"
TEST_MISSING: LlmTestStatus = "missing"


@dataclass(frozen=True)
class Review:
    """O que a publicação do rascunho faria e o que falta para ela, antes de gerar o PDF."""

    draft: Draft
    number: int  # a versão que a publicação cria
    document: dict[str, Any]  # a definição que vira a versão, com a versão do consentimento nova
    changes: tuple[SectionChange, ...]  # contra a versão atual, seção por seção
    problems: tuple[Problem, ...]  # a definição não compila
    llm_changes: tuple[str, ...]  # o que mudou e vai ao LLM; vazio: não precisa de teste
    tested_at: datetime | None  # o teste com sucesso que cobre o que vai ao LLM agora
    contact: tuple[ContactRole, ...]  # os papéis da identidade que o prompt manda ao LLM
    consent_version: str | None  # a versão nova do texto do consentimento, se o texto mudou
    spec: FormSpec | None  # o contrato compilado da versão nova

    @property
    def nothing(self) -> bool:
        return not self.draft.pending

    @property
    def first(self) -> bool:
        """A primeira publicação, que cria a versão 1 e põe o formulário no ar."""
        return not self.draft.published

    @property
    def needs_test(self) -> bool:
        return bool(self.llm_changes) and self.tested_at is None

    @property
    def test_status(self) -> LlmTestStatus:
        """O que a publicação pede do teste do que vai ao LLM: `TEST_NOT_NEEDED` (nada que vai ao
        LLM mudou), `TEST_COVERED` (mudou, e um teste com sucesso depois da mudança o cobre) ou
        `TEST_MISSING` (mudou, e falta o teste). É a mesma regra de `draft_tests.gate_of`, que diz o
        que cada teste guardado vale: `current` e `unlocks` cobrem, `stale` não."""
        if not self.llm_changes:
            return TEST_NOT_NEEDED
        return TEST_COVERED if self.tested_at is not None else TEST_MISSING

    def dropped_values(self) -> tuple[DroppedValue, ...]:
        """Os valores de opção da versão publicada que a versão nova não grava mais na mesma pergunta
        (os envios novos não os trazem, e o receptor do webhook pode depender deles). Vazio antes da
        primeira publicação."""
        if self.draft.current is None:
            return ()
        return dropped_option_values(self.draft.current.definition, self.document)

    def blockers(self) -> list[str]:
        """O que falta para publicar e não depende de quem publica: nada para publicar, a definição
        que não compila e o teste do que vai ao LLM. A nota e a confirmação dos dados de contato
        são de quem publica (`reasons`)."""
        if self.nothing:
            return [NOTHING_TO_PUBLISH]
        found = []
        if self.problems:
            found.append(DOES_NOT_COMPILE)
        if self.needs_test and self.first:
            found.append(FIRST_TEST_REQUIRED)
        elif self.needs_test:
            found.append(TEST_REQUIRED.format(changes=changed(self.llm_changes)))
        return found

    def reasons(self, *, note: str, contact_confirmed: bool) -> list[str]:
        """O que falta para publicar, tudo de uma vez: o que `blockers` diz e, se a publicação não
        está vazia, a confirmação dos dados de contato e a nota."""
        found = self.blockers()
        if self.nothing:
            return found
        if self.contact and not contact_confirmed:
            found.append(CONTACT_UNCONFIRMED)
        if not note.strip():
            found.append(NOTE_REQUIRED)
        return found


def changed(parts: Sequence[str]) -> str:
    """O que mudou, numa frase, com o verbo no número do que mudou: "mudou o prompt", "mudaram as
    perguntas que o prompt cita", "mudaram o prompt e as instruções"."""
    listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} e {parts[-1]}"
    plural = len(parts) > 1 or parts[0].startswith(("os ", "as "))
    return f"{'mudaram' if plural else 'mudou'} {listed}"


class PublishRefused(Exception):
    """A publicação não aconteceu: `reasons` diz o que falta, e `problems`, os problemas da
    definição ou do PDF de exemplo. Nada foi gravado."""

    def __init__(
        self, review: Review, reasons: Sequence[str], problems: Sequence[Problem] = ()
    ) -> None:
        super().__init__("; ".join(reasons))
        self.review = review
        self.reasons = tuple(reasons)
        self.problems = tuple(problems) or review.problems

    @property
    def only_contact_missing(self) -> bool:
        """Só falta a confirmação dos dados de contato no prompt: a definição vale, o teste e a nota
        estão em dia, e basta a pessoa dizer que sabe o que o prompt manda ao LLM."""
        return self.reasons == (CONTACT_UNCONFIRMED,)


@dataclass(frozen=True)
class Published:
    """A versão que a publicação criou, com a revisão que ela seguiu (as mudanças, o que ia ao LLM,
    os dados de contato confirmados)."""

    slug: str
    title: str
    number: int
    review: Review


@dataclass(frozen=True)
class Simulation:
    """O que a publicação faria agora (`publish(dry_run=True)`), sem gravar: a revisão, tudo o que
    falta (`reasons`, na ordem da recusa, mais o PDF de exemplo que o adaptador não gerou) e os
    problemas da definição ou do PDF. Pronta (`ready`) quando nada falta: publicar agora, com a
    mesma nota e a mesma confirmação, daria certo."""

    review: Review
    reasons: tuple[str, ...]
    problems: tuple[Problem, ...]

    @property
    def ready(self) -> bool:
        return not self.reasons and not self.problems


def _tested_at(session: Session, draft: Draft) -> datetime | None:
    """Quando terminou o teste com sucesso que cobre o que vai ao LLM no rascunho: a prova
    guardada no rascunho ou um dos testes guardados."""
    target = llm_fingerprint(draft.document)
    if draft.tested_fingerprint == target:
        return draft.tested_at
    tested: datetime | None = session.scalar(
        sa.select(sa.func.max(DraftTest.finished_at)).where(
            DraftTest.form_id == draft.form_id,
            DraftTest.status == "succeeded",
            DraftTest.llm_fingerprint == target,
        )
    )
    return tested


def review(session: Session, draft: Draft) -> Review:
    """O que a publicação do rascunho faria, sem o PDF de exemplo, que só a publicação gera. A
    primeira publicação compara com uma definição vazia: tudo é novo, inclusive o que vai ao LLM,
    e a versão do texto do consentimento é a da versão 1 (`<slug>-v1`)."""
    number = draft.next_number
    previous = draft.current.definition if draft.current is not None else {}
    bumped = consent_version(draft.document, previous, number=number)
    document = dict(draft.document)
    if bumped is not None:
        document["consent"] = {**document["consent"], "version": bumped}
    problems: tuple[Problem, ...] = ()
    spec: FormSpec | None = None
    try:
        spec = compile_definition(document, version_number=number)
    except DefinitionError as exc:
        problems = exc.problems
    if spec is not None:
        from app.integration_readiness import problems as integration_problems

        problems = (*problems, *integration_problems(session, spec))
    llm = (
        llm_changes(previous, draft.document)
        if draft.pending and document.get("generation", "pdf") == "pdf"
        else ()
    )
    return Review(
        draft=draft,
        number=number,
        document=document,
        changes=compare(previous, document),
        problems=problems,
        llm_changes=llm,
        tested_at=_tested_at(session, draft) if llm else None,
        contact=tuple(contact_in_prompt(spec, document["identity"])) if spec else (),
        consent_version=bumped if draft.current is not None else None,
        spec=spec,
    )


@overload
def publish(
    deps: Deps,
    slug: str,
    *,
    actor: str,
    note: str,
    expected: str,
    contact_confirmed: bool = False,
    dry_run: Literal[False] = False,
) -> Published: ...


@overload
def publish(
    deps: Deps,
    slug: str,
    *,
    actor: str,
    note: str,
    expected: str,
    contact_confirmed: bool = False,
    dry_run: Literal[True],
) -> Simulation: ...


def publish(
    deps: Deps,
    slug: str,
    *,
    actor: str,
    note: str,
    expected: str,
    contact_confirmed: bool = False,
    dry_run: bool = False,
) -> Published | Simulation:
    """Publica o rascunho como a versão seguinte, em nome de `actor`, com a nota. `expected` é o
    `Draft.token` que a página mostrou. Pede o mesmo que a importação (a definição como o banco a
    guarda compila, a confirmação dos dados de contato no prompt e o PDF de exemplo no adaptador
    do PDF), a nota e, se mudou algo que vai ao LLM, um teste com sucesso depois da mudança.

    A versão nova vale já para os envios novos, sem reiniciar. A situação do formulário não muda,
    o nome dele no admin (`forms.title`) passa a ser o título da versão e o rascunho sai. Levanta
    `UnknownForm`, `DraftChanged` ou `PublishRefused`; nos dois últimos, nada é gravado.

    Com `dry_run`, roda as mesmas conferências, inclusive o PDF de exemplo no adaptador (que custa
    CPU), e devolve a `Simulation` do que faria: **não grava, não audita e não levanta
    `PublishRefused`**, porque o que falta é a resposta. `UnknownForm` e `DraftChanged` valem
    igual. O PDF é renderizado mesmo com outros motivos de recusa, para a lista sair completa; só
    não é quando não há o que publicar ou a definição não compila."""
    with deps.sessions() as session:
        draft = load(session, slug)
        if draft is None:
            raise UnknownForm(slug)
        if draft.token != expected:
            raise DraftChanged
        found = review(session, draft)
    reasons = found.reasons(note=note, contact_confirmed=contact_confirmed)
    if dry_run:
        return _simulation(deps, slug, found, reasons)
    if reasons:
        raise PublishRefused(found, reasons)
    problems = _pdf_problems(deps, slug, found)
    if problems:
        raise PublishRefused(found, [_publication_reason(problems)], problems)
    assert found.spec is not None  # sem problemas, a definição compilou
    digest = fingerprint(found.document)
    now = utc_now()
    version_id = uuid.uuid4()
    try:
        with deps.sessions.begin() as session:
            from app.integration_readiness import configuration_lock
            from app.integration_readiness import problems as integration_problems

            configuration_lock(session)
            from app.forms.continuation_contract import assert_definition_contract

            assert_definition_contract(session, found.document)
            if issues := integration_problems(session, found.spec):
                raise PublishRefused(found, ["Integrações incompatíveis com a finalidade."], issues)
            session.execute(sa.select(Form.id).where(Form.id == draft.form_id).with_for_update())
            latest = load(session, slug)
            if latest is None or latest.token != expected or latest.next_number != found.number:
                raise DraftChanged
            session.add(
                FormVersion(
                    id=version_id,
                    form_id=draft.form_id,
                    number=found.number,
                    definition=found.document,
                    fingerprint=digest,
                    # O Postgres não guarda o caractere nulo num texto.
                    note=note.replace("\x00", "").strip()[:NOTE_MAX_LENGTH],
                    published_by=actor,
                    published_at=now,
                )
            )
            try:  # fixa o conteúdo de cada imagem que a versão usa
                pinned = form_assets.pin_version(session, version_id, slug, found.document)
            except form_assets.MissingImages as missing:  # uma imagem saiu no meio do caminho
                path = missing.path(found.document)
                problems = [Problem(path, missing.message())]
                reason = OPENING_LOGO_MISSING if path == "opening_logo.src" else IMAGE_GONE
                raise PublishRefused(found, [reason], problems) from None
            session.execute(
                sa.update(Form).where(Form.id == draft.form_id).values(title=found.spec.title)
            )
            session.execute(sa.delete(FormDraft).where(FormDraft.form_id == draft.form_id))
            details: dict[str, Any] = {
                "form_slug": slug,
                "version": found.number,
                "fingerprint": digest,
            }
            if pinned:
                details["images"] = {name: ref.sha256 for name, ref in pinned.items()}
            if found.llm_changes and found.tested_at is not None:
                details["tested_at"] = found.tested_at.isoformat()
            if found.contact:
                details["contact_in_prompt"] = list(found.contact)
            if found.consent_version is not None:
                details["consent_version"] = found.consent_version
            session.add(
                AuditLog(
                    actor=actor,
                    action="form.publish",
                    target_type="form",
                    target_id=str(draft.form_id),
                    details=details,
                )
            )
    except IntegrityError:  # outra publicação passou na frente
        raise DraftChanged from None
    return Published(slug, found.spec.title, found.number, found)


def _publication_reason(problems: Sequence[Problem]) -> str:
    return OPENING_LOGO_MISSING if problems[0].path == "opening_logo.src" else PDF_FAILED


def _pdf_problems(deps: Deps, slug: str, found: Review) -> list[Problem]:
    """O que o PDF de exemplo da versão nova não passa: as imagens que ele usa e o adaptador do PDF
    (o WeasyPrint em produção), que confere o que a compilação não vê."""
    assert found.spec is not None  # quem chama já viu a definição compilar
    with deps.sessions() as session:
        library = {name: asset.ref for name, asset in form_assets.visible(session, slug).items()}
    return publication_problems(found.spec, found.document, deps.pdf, assets=library)


def _simulation(deps: Deps, slug: str, found: Review, reasons: Sequence[str]) -> Simulation:
    """A simulação: as razões da recusa e, se a definição compila e há o que publicar, o PDF de exemplo
    no adaptador, que acrescenta o que a recusa diria dele."""
    problems: Sequence[Problem] = found.problems
    if found.spec is not None and not found.nothing:
        from_pdf = _pdf_problems(deps, slug, found)
        if from_pdf:
            reasons, problems = [*reasons, _publication_reason(from_pdf)], from_pdf
    return Simulation(found, tuple(reasons), tuple(problems))


@dataclass(frozen=True)
class CaptureTest:
    """Teste local sem gravar respostas ou chamar geração."""

    spec: FormSpec
    today: date
    scores: dict[str, Any]
    completion: Completion


def test_capture(draft: Draft, *, answers=None, day="", now: datetime) -> CaptureTest:
    """Valida respostas, data, pontuação e conclusão na mesma versão do rascunho."""
    moment = simulated_moment(_simulated(day), now)
    scored = score(draft, answers=answers, now=moment)
    if scored.spec.generation != "none":
        raise GenerationUnavailable("este teste local exige somente captura")
    result = scored.run.result.scores if scored.run else {}
    return CaptureTest(scored.spec, local_date(moment), result, scored.spec.completion)
