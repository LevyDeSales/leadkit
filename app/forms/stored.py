"""Os formulários guardados no banco (ADR 0013): o registro que os resolve, a importação de uma
definição, que cria o formulário e publica a versão 1, e a situação de cada formulário, ativo ou
inativo.

O registro (`StoredRegistry`) é o `FormRegistry` do app, em `Deps.forms`:
- `current(slug)` consulta, a cada chamada e numa consulta só pelo índice, a situação e o número
  da versão atual, a de maior número. Uma publicação e uma desativação valem já no pedido
  seguinte, sem reiniciar o app. Formulário inativo levanta `InactiveForm`;
- `get(slug, número)` compila a versão (`compile_definition`) quando ela é pedida, junto com as
  imagens que ela fixou na publicação (`app.forms.assets`), e guarda o contrato em cache, por slug
  e número. O cache tem um teto de versões: a que sai dele é
  compilada de novo no pedido seguinte. Uma versão publicada não muda, então o contrato é o mesmo.
  A situação não conta aqui: os pedidos que já existem seguem a versão deles;
- uma versão guardada que não compila mais (o compilador mudou num deploy) responde como
  desconhecida (`UnknownForm`). O log diz só o slug, o número e a contagem de problemas, nunca o
  conteúdo da definição.

A importação (`import_definition`) confere a definição como o banco a guarda, pede confirmação
quando o prompt cita dados de contato da lead e grava o formulário, a versão 1 e a auditoria numa
transação só. As versões seguintes nascem do rascunho, com as mesmas conferências
(`app.forms.drafts`). A situação (`set_active`) mora em `forms`.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.pdf import PdfRenderer
from app.errors import RetryableError, TerminalError
from app.forms import FormSpec, InactiveForm, UnknownForm
from app.forms import assets as form_assets
from app.forms.definition import (
    DefinitionError,
    Problem,
    compile_definition,
    fingerprint,
    utc_now,
)
from app.forms.definition.document import (
    PATTERN_HINTS,
    SLUG_MAX_LENGTH,
    SLUG_PATTERN,
    identifier_of,
)
from app.forms.definition.images import AssetRef
from app.forms.definition.resources import resource_problems
from app.models import AuditLog, Form, FormVersion, WebhookDestination

if TYPE_CHECKING:
    from app.deps import Deps

logger = logging.getLogger(__name__)

# Quantas versões compiladas cada processo guarda. Passou disso, sai a usada há mais tempo, e um
# pedido seguinte a compila de novo.
CACHE_SIZE = 64
FIRST_VERSION = 1
DEFAULT_NOTE = "Importação da definição."
NOTE_MAX_LENGTH = 300
# Palavras que não servem de endereço de formulário. `import` e `new` são páginas do admin
# (`/admin/forms/<palavra>`): um formulário com esse slug ficaria sem a página dele. `default` é o
# escopo do destino padrão na API de admin (`/destinations/default`, ADR 0015): a palavra ocupa o lugar
# do endereço, e um formulário `default` se confundiria com o destino padrão. Um teste lê as rotas do
# admin e as da API e cobra aqui toda palavra nova.
RESERVED_SLUGS = frozenset({"import", "new", "default"})
SLUG_RESERVED = "este endereço é reservado; escolha outro"
SLUG_IN_DATABASE = "já existe um formulário com este endereço"
SLUG_HAS_DESTINATION = (
    "este endereço ainda tem um destino de webhook guardado, de um formulário que não existe mais; "
    "remova o destino em Destinos ou escolha outro endereço"
)
# Os papéis da identidade da lead que são dados de contato: o prompt que os cita pede confirmação.
ContactRole = Literal["name", "email", "phone"]
CONTACT_ROLES: tuple[ContactRole, ...] = ("name", "email", "phone")
CONTACT_ROLE_WORDS = {"name": "o nome", "email": "o e-mail", "phone": "o telefone"}


class StoredRegistry:
    """O registro do app: os formulários guardados no banco, lidos pelas `sessions` do processo.
    `cache_size` diz quantas versões compiladas ficam em memória. Seguro entre threads."""

    def __init__(self, sessions: sessionmaker[Session], *, cache_size: int = CACHE_SIZE) -> None:
        self._sessions = sessions
        self._cache_size = cache_size
        # None: a versão existe, mas não compila. Fica em cache também: ela não muda.
        self._compiled: OrderedDict[tuple[str, int], FormSpec | None] = OrderedDict()
        self._lock = threading.Lock()

    def current(self, slug: str) -> FormSpec:
        latest = (
            sa.select(sa.func.max(FormVersion.number))
            .where(FormVersion.form_id == Form.id)
            .scalar_subquery()
        )
        with self._sessions() as session:
            row = session.execute(
                sa.select(Form.active, latest).where(Form.slug == slug)
            ).one_or_none()
        if row is None or row[1] is None:  # sem linha, ou sem versão publicada
            raise UnknownForm(slug)
        active, number = row
        if not active:
            raise InactiveForm(slug)
        return self.get(slug, number)

    def get(self, slug: str, version_number: int) -> FormSpec:
        key = (slug, version_number)
        with self._lock:
            if key in self._compiled:
                self._compiled.move_to_end(key)
                return _found(self._compiled[key], key)
        compiled = self._compile(slug, version_number)
        with self._lock:
            # Outra thread pode ter compilado a mesma versão no meio do caminho: vale a primeira, e
            # todo pedido recebe o mesmo contrato.
            compiled = self._compiled.setdefault(key, compiled)
            self._compiled.move_to_end(key)
            while len(self._compiled) > self._cache_size:
                self._compiled.popitem(last=False)
        return _found(compiled, key)

    def current_forms(self) -> list[FormSpec]:
        with self._sessions() as session:
            stored = session.execute(
                sa.select(Form.slug, sa.func.max(FormVersion.number))
                .join(FormVersion, FormVersion.form_id == Form.id)
                .group_by(Form.slug)
                .order_by(Form.slug)
            ).all()
        forms = []
        for slug, number in stored:
            try:
                forms.append(self.get(slug, number))
            except UnknownForm:  # não compila mais: o log já disse qual
                continue
        return forms

    def _compile(self, slug: str, number: int) -> FormSpec | None:
        """A versão compilada; None se ela não compila. Levanta `UnknownForm` se ela não existe
        (o que não fica em cache: a versão pode ser publicada depois)."""
        with self._sessions() as session:
            definition = session.scalar(
                sa.select(FormVersion.definition)
                .join(Form, Form.id == FormVersion.form_id)
                .where(Form.slug == slug, FormVersion.number == number)
            )
        if definition is None:
            raise UnknownForm(slug, number)
        try:
            spec = compile_definition(definition, version_number=number)
        except DefinitionError as exc:
            logger.error(
                "formulário %s versão %s: a definição guardada não compila (problemas: %s)",
                slug,
                number,
                len(exc.problems),
            )
            return None
        with self._sessions() as session:
            pinned = form_assets.version_refs(session, slug, number)
        return spec.with_assets(pinned) if pinned else spec


def _found(spec: FormSpec | None, key: tuple[str, int]) -> FormSpec:
    if spec is None:
        raise UnknownForm(*key)
    return spec


# --- importação -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Imported:
    """O formulário que a importação criou, com a versão publicada."""

    slug: str
    title: str
    version_number: int


class ContactInPrompt(Exception):
    """O prompt cita dados de contato da lead: `roles` diz quais, pelo papel na identidade (`name`,
    `email`, `phone`). Publicar assim pede a confirmação de quem publica."""

    def __init__(self, roles: Sequence[str]) -> None:
        super().__init__(", ".join(roles))
        self.roles = tuple(roles)


def import_definition(
    deps: Deps,
    text: str | bytes,
    *,
    actor: str,
    note: str = "",
    contact_confirmed: bool = False,
) -> Imported:
    """Cria o formulário da definição em JSON e publica a versão 1, em nome de `actor`, com a nota
    (`DEFAULT_NOTE` se vier vazia). Levanta `DefinitionError` com os problemas, ou
    `ContactInPrompt` se o prompt cita dados de contato e `contact_confirmed` não veio; nos dois
    casos, nada é gravado.

    A conferência vale para a definição como o banco a guarda, que é a que o registro compila
    depois: o JSONB reordena as chaves dos objetos e escreve os números do jeito dele. A primeira
    compilação, a do texto recebido, só aponta os problemas com a linha do texto."""
    spec = compile_definition(text, version_number=FIRST_VERSION)
    with deps.sessions() as session:
        taken = _slug_taken(session, spec.slug)
        document = as_stored(session, json.loads(text))  # a compilação já leu o mesmo texto
        library = {
            name: asset.ref for name, asset in form_assets.visible(session, spec.slug).items()
        }
    if taken is not None:
        raise DefinitionError([Problem("slug", taken)])
    spec = compile_definition(document, version_number=FIRST_VERSION)
    cited = contact_in_prompt(spec, document["identity"])
    if cited and not contact_confirmed:
        raise ContactInPrompt(cited)
    problems = publication_problems(spec, document, deps.pdf, assets=library)
    if problems:
        raise DefinitionError(problems)
    digest = fingerprint(document)
    now = utc_now()
    version_id = uuid.uuid4()
    with deps.sessions.begin() as session:
        from app.integration_readiness import configuration_lock
        from app.integration_readiness import problems as integration_problems

        configuration_lock(session)
        from app.forms.continuation_contract import assert_definition_contract

        assert_definition_contract(session, document)
        if issues := integration_problems(session, spec):
            raise DefinitionError(issues)
        form_id = session.scalar(
            insert(Form)
            .values(
                id=uuid.uuid4(),
                slug=spec.slug,
                title=spec.title,
                created_by=actor,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[Form.slug])
            .returning(Form.id)
        )
        if form_id is None:  # outra importação do mesmo slug passou na frente
            raise DefinitionError([Problem("slug", SLUG_IN_DATABASE)])
        session.add(
            FormVersion(
                id=version_id,
                form_id=form_id,
                number=FIRST_VERSION,
                definition=document,
                fingerprint=digest,
                note=_note(note),
                published_by=actor,
                published_at=now,
            )
        )
        try:  # as imagens que a definição usa ficam fixadas no conteúdo de agora
            form_assets.pin_version(session, version_id, spec.slug, document)
        except form_assets.MissingImages as missing:  # uma imagem saiu no meio do caminho
            raise DefinitionError([Problem("pdf", missing.message())]) from None
        # Quem confirmou mandar dados de contato ao LLM fica registrado (os papéis, nunca valores).
        confirmed = {"contact_in_prompt": cited} if cited else {}
        session.add(
            AuditLog(
                actor=actor,
                action="form.import",
                target_type="form",
                target_id=str(form_id),
                details={
                    "form_slug": spec.slug,
                    "version": FIRST_VERSION,
                    "fingerprint": digest,
                    **confirmed,
                },
            )
        )
    return Imported(spec.slug, spec.title, FIRST_VERSION)


def contact_in_words(roles: Sequence[str]) -> str:
    """Os papéis de contato por extenso: `("name", "email")` é "o nome e o e-mail". É como a página e
    a API dizem à pessoa que confirma o que o prompt manda ao LLM."""
    words = [CONTACT_ROLE_WORDS.get(role, role) for role in roles]
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} e {words[-1]}"


def as_stored(session: Session, document: Any) -> dict[str, Any]:
    """O documento como o banco o devolve depois de guardar num JSONB."""
    stored: dict[str, Any] = session.scalar(sa.select(sa.literal(document, JSONB)))
    return stored


def contact_in_prompt(spec: FormSpec, identity: Mapping[str, str | None]) -> list[ContactRole]:
    """Os papéis da identidade da lead (`CONTACT_ROLES`) cujas perguntas o prompt cita. Quando o
    prompt usa `answers` de um jeito que não dá para seguir (inteiro, num `set`, com a chave
    calculada), qualquer resposta pode ir ao LLM: contam todos os papéis que o formulário tem."""
    if spec.generation == "none":
        return []
    assert spec.templates is not None  # um formulário PDF compilado tem os templates
    read = spec.templates.prompt.reads("answers")
    return [
        role
        for role in CONTACT_ROLES
        if identity.get(role) is not None and (read is None or identity.get(role) in read)
    ]


def publication_problems(
    spec: FormSpec,
    document: Mapping[str, Any],
    pdf: PdfRenderer,
    *,
    assets: Mapping[str, AssetRef] | None = None,
) -> list[Problem]:
    """O que uma versão precisa passar para ser publicada, além de compilar: o PDF não pede nada de
    fora de `app/assets`, das imagens do formulário e de `data:` (com a linha, o que a renderização
    não diz), toda imagem que os templates pedem existe em `assets` (as imagens que o formulário
    enxerga, pelo nome; sem elas, nenhuma existe) e o PDF de exemplo, com as respostas e o plano de
    exemplo, sai do adaptador do PDF (o WeasyPrint em produção), que confere o que a compilação não
    vê, inclusive o arquivo de cada imagem. A compilação já renderizou o prompt, a prévia e o HTML
    do PDF com os exemplos. `document` é a definição como o banco a guarda."""
    if spec.generation == "none":
        return []
    known = assets or {}
    pdf_texts = document["pdf"]
    outside = resource_problems(
        pdf_texts["html"],
        pdf_texts.get("css", ""),
        preview=document.get("preview", ""),
        assets=known.keys(),
    )
    if outside:
        return outside
    answers = spec.answers_model.model_validate(document["examples"]["answers"])
    context = {
        "lead": {"name": spec.identity(answers).name},
        "form": {"slug": spec.slug, "title": spec.title},
        # As notas das respostas de exemplo, como o envio de verdade as entrega ao template.
        "scores": spec.scoring.run(answers).result.scores if spec.scoring is not None else {},
        "generated_at": utc_now(),
    }
    used = {name: known[name] for name in form_assets.used_names(document) if name in known}
    try:
        pdf.render(spec.with_assets(used), spec.example_plan, context)
    except (TerminalError, RetryableError) as exc:  # a falha passageira do storage conta também
        return [Problem("pdf", f"o PDF de exemplo não foi gerado: {exc.detail}")]
    return []


def _slug_taken(session: Session, slug: str) -> str | None:
    """Por que o slug não serve para um formulário novo; None se ele está livre. O destino de
    webhook guardado de um formulário que não existe mais também o prende: o formulário novo
    herdaria o destino, e os pedidos dele iriam para o sistema do antigo."""
    if slug in RESERVED_SLUGS:
        return SLUG_RESERVED
    if session.scalar(sa.select(Form.id).where(Form.slug == slug)) is not None:
        return SLUG_IN_DATABASE
    kept = sa.select(WebhookDestination.id).where(WebhookDestination.form_slug == slug)
    if session.scalar(kept) is not None:
        return SLUG_HAS_DESTINATION
    return None


def slug_problem(session: Session, slug: str) -> str | None:
    """Por que o slug não serve de endereço para um formulário novo (o formato do compilador, as
    páginas do admin ou outro formulário); None se ele serve."""
    if len(slug) > SLUG_MAX_LENGTH:
        return f"use no máximo {SLUG_MAX_LENGTH} caracteres"
    if not re.fullmatch(SLUG_PATTERN, slug):
        return PATTERN_HINTS[SLUG_PATTERN]
    return _slug_taken(session, slug)


def slug_of(title: str) -> str:
    """Um endereço tirado do título, com hífens entre as palavras (`identifier_of`)."""
    return identifier_of(title, separator="-", max_length=SLUG_MAX_LENGTH)


def _note(note: str) -> str:
    return note.strip()[:NOTE_MAX_LENGTH] or DEFAULT_NOTE


# --- situação ---------------------------------------------------------------------------------

ACTIVATION_ACTIONS = {True: "form.activate", False: "form.deactivate"}  # na auditoria


def set_active(sessions: sessionmaker[Session], slug: str, active: bool, *, actor: str) -> bool:
    """Ativa ou desativa o formulário em nome de `actor`, com uma entrada na auditoria. Devolve
    se a situação mudou; False se ela já era essa (um clique repetido, outra aba). Levanta
    `UnknownForm` se o slug não é de formulário nenhum."""
    with sessions.begin() as session:
        form_id = session.scalar(
            sa.update(Form)
            .where(Form.slug == slug, Form.active != active)
            .values(active=active)
            .returning(Form.id)
        )
        if form_id is None:  # nada mudou: o formulário já estava assim, ou não existe
            if not session.scalar(sa.select(Form.id).where(Form.slug == slug)):
                raise UnknownForm(slug)
            return False
        session.add(
            AuditLog(
                actor=actor,
                action=ACTIVATION_ACTIONS[active],
                target_type="form",
                target_id=str(form_id),
                details={"form_slug": slug},
            )
        )
    return True


@dataclass(frozen=True)
class StoredForm:
    """Um formulário com linha em `forms`, como o admin lista."""

    slug: str
    title: str
    active: bool
    version_number: int | None  # a versão atual; None se nenhuma foi publicada
    published_at: datetime | None


def listing(session: Session, slug: str | None = None) -> list[StoredForm]:
    """As linhas de `forms` (só a do `slug`, se vier), por slug, com a versão atual de cada uma."""
    latest = (
        sa.select(FormVersion.form_id, sa.func.max(FormVersion.number).label("number"))
        .group_by(FormVersion.form_id)
        .subquery()
    )
    statement = (
        sa.select(Form.slug, Form.title, Form.active, latest.c.number, FormVersion.published_at)
        .outerjoin(latest, latest.c.form_id == Form.id)
        .outerjoin(
            FormVersion,
            sa.and_(FormVersion.form_id == Form.id, FormVersion.number == latest.c.number),
        )
        .order_by(Form.slug)
    )
    if slug is not None:
        statement = statement.where(Form.slug == slug)
    return [StoredForm(*row) for row in session.execute(statement)]


@dataclass(frozen=True)
class StoredVersion:
    """Uma versão publicada de um formulário do banco, com a definição como o banco a guarda."""

    slug: str
    number: int
    definition: dict[str, Any]
    fingerprint: str
    note: str
    published_by: str
    published_at: datetime

    @property
    def title(self) -> str:
        return str(self.definition["title"])


def stored_version(session: Session, slug: str, number: int | None = None) -> StoredVersion | None:
    """A versão `number` do formulário do banco com este slug, ou a atual (a de maior número) se
    `number` não vier; None se ela não existe."""
    statement = (
        sa.select(
            Form.slug,
            FormVersion.number,
            FormVersion.definition,
            FormVersion.fingerprint,
            FormVersion.note,
            FormVersion.published_by,
            FormVersion.published_at,
        )
        .join(FormVersion, FormVersion.form_id == Form.id)
        .where(Form.slug == slug)
        .order_by(FormVersion.number.desc())
        .limit(1)
    )
    if number is not None:
        statement = statement.where(FormVersion.number == number)
    row = session.execute(statement).one_or_none()
    return StoredVersion(*row) if row is not None else None


def current_versions(session: Session) -> list[StoredVersion]:
    """A versão atual (a de maior número) de cada formulário publicado, por slug, com a definição
    como o banco a guarda. Um formulário sem versão publicada não entra."""
    latest = (
        sa.select(FormVersion.form_id, sa.func.max(FormVersion.number).label("number"))
        .group_by(FormVersion.form_id)
        .subquery()
    )
    statement = (
        sa.select(
            Form.slug,
            FormVersion.number,
            FormVersion.definition,
            FormVersion.fingerprint,
            FormVersion.note,
            FormVersion.published_by,
            FormVersion.published_at,
        )
        .join(FormVersion, FormVersion.form_id == Form.id)
        .join(
            latest,
            sa.and_(latest.c.form_id == FormVersion.form_id, latest.c.number == FormVersion.number),
        )
        .order_by(Form.slug)
    )
    return [StoredVersion(*row) for row in session.execute(statement)]


@dataclass(frozen=True)
class VersionEntry:
    """Uma versão publicada no histórico de um formulário do banco, sem a definição."""

    number: int
    note: str
    published_by: str
    published_at: datetime
    fingerprint: str


def history(session: Session, slug: str) -> list[VersionEntry]:
    """As versões publicadas do formulário do banco com este slug, a mais recente primeiro."""
    statement = (
        sa.select(
            FormVersion.number,
            FormVersion.note,
            FormVersion.published_by,
            FormVersion.published_at,
            FormVersion.fingerprint,
        )
        .join(Form, Form.id == FormVersion.form_id)
        .where(Form.slug == slug)
        .order_by(FormVersion.number.desc())
    )
    return [VersionEntry(*row) for row in session.execute(statement)]
