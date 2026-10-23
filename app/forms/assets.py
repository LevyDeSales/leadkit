"""As imagens dos formulários: enviar, trocar, remover, listar e fixar na publicação.

Imagem de formulário é conteúdo, e mora no storage, não no repositório. Os templates do PDF e da
prévia a pedem por nome (`asset:<nome>`, ver `app.forms.definition.images`).

Escopo e nome
-------------
Cada imagem tem um nome curto, único no escopo dela: a biblioteca geral (que todo formulário
enxerga) ou um formulário (só dele). Um formulário que tem uma imagem com o mesmo nome de uma da
biblioteca usa a dele.

Onde fica cada coisa
--------------------
- Os bytes ficam no storage, pela chave do conteúdo (`assets/<sha256>.<extensão>`, ver
  `app.adapters.assets`): imutáveis, sem duplicata. Enviar de novo a mesma imagem não grava nada
  de novo.
- Os metadados (`FormAsset`): o escopo, o nome, o tipo, o tamanho antes e depois da otimização, as
  dimensões, o sha256 e quem enviou e trocou.
- O que cada versão publicada usa (`FormVersionAsset`): o sha256 e o tipo de cada imagem que o
  PDF, o CSS ou a prévia dela pedem, fixados na publicação. O PDF de um pedido antigo sai igual
  mesmo depois de a imagem ser trocada, porque a versão dele segue com o conteúdo que fixou. O
  rascunho, ao contrário, sempre usa as imagens de agora.

Enviar, trocar e remover
------------------------
- Enviar otimiza (`app.forms.image_optimizer`), guarda os bytes e grava a linha, com a auditoria.
- Trocar muda o conteúdo e mantém o nome. As versões publicadas continuam com o conteúdo antigo;
  o rascunho que usa a imagem passa a ter uma mudança para publicar (`stale_names`), porque os
  envios novos só pegam a imagem nova na versão seguinte.
- Remover é recusado enquanto uma versão publicada usa a imagem (`AssetInUse`): o banco também não
  deixa. O arquivo no storage sai quando nenhuma imagem nem versão aponta mais para o conteúdo.

Toda mudança fica na auditoria (`asset.upload`, `asset.replace` e `asset.remove`), com o escopo, o
nome, o sha256 e os tamanhos, nunca os bytes.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.assets import AssetStore
from app.errors import RetryableError, TerminalError
from app.forms.definition.images import AssetRef, name_problem
from app.forms.definition.resources import UNKNOWN_IMAGE, asset_names
from app.forms.image_optimizer import ImageRefused, OptimizedImage, optimize
from app.models import AuditLog, Form, FormAsset, FormVersion, FormVersionAsset, utcnow

if TYPE_CHECKING:
    from app.deps import Deps

logger = logging.getLogger(__name__)

NAME_TAKEN = "já existe uma imagem com este nome; troque a imagem que existe ou use outro nome"
UNKNOWN_SCOPE = "formulário não encontrado; escolha outro"
NO_FILE = "escolha o arquivo da imagem"
CHANGED = (
    "A imagem mudou desde que você abriu esta página (outra aba ou outra pessoa). Nada foi "
    "alterado: confira os dados abaixo e tente de novo."
)
STORAGE_FAILED = (
    "Não deu para guardar a imagem no storage ({detail}). Nada foi gravado: tente de novo."
)
IN_USE_NOW = "Esta imagem não pode ser removida: uma versão publicada passou a usá-la agora."


def storage_failure(failed: RetryableError | TerminalError) -> str:
    """O que dizer quando o storage não guardou a imagem (enviar ou trocar): a causa, que o adaptador
    já traduziu sem dado de ninguém, e que nada foi gravado."""
    return STORAGE_FAILED.format(detail=failed.detail)


class AssetRefused(Exception):
    """O envio não vale: `field` é o campo da tela com o problema (`name`, `scope` ou `file`) e
    `message`, o motivo. Nada é gravado."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class UnknownAsset(LookupError):
    """Não há imagem com este id."""


class AssetChanged(Exception):
    """A imagem mudou desde que a página foi aberta: nada foi gravado."""


@dataclass(frozen=True)
class Use:
    """Uma versão publicada que usa a imagem."""

    form_slug: str
    number: int


class AssetInUse(Exception):
    """Uma versão publicada usa a imagem, e por isso ela não sai. `uses` diz quais versões."""

    def __init__(self, uses: Sequence[Use]) -> None:
        super().__init__(", ".join(f"{use.form_slug} v{use.number}" for use in uses))
        self.uses = tuple(uses)

    def message(self) -> str:
        """Por que a imagem não sai: quais versões publicadas a usam (a tela e a API dizem o mesmo)."""
        if not self.uses:
            return IN_USE_NOW
        listed = ", ".join(f"{use.form_slug} v{use.number}" for use in self.uses)
        who = "A versão publicada" if len(self.uses) == 1 else "As versões publicadas"
        verb = "usa" if len(self.uses) == 1 else "usam"
        return (
            f"Esta imagem não pode ser removida: {who} {listed} {verb} esta imagem. Para mudá-la, "
            "troque o arquivo."
        )


class MissingImages(Exception):
    """Os templates pedem imagens que não existem (mais): `names` diz quais."""

    def __init__(self, names: Sequence[str]) -> None:
        super().__init__(", ".join(names))
        self.names = tuple(names)

    def message(self) -> str:
        return "; ".join(UNKNOWN_IMAGE.format(name=name) for name in self.names)


@dataclass(frozen=True)
class Asset:
    """Uma imagem como o admin a mostra."""

    id: uuid.UUID
    name: str
    form_slug: str | None  # None: a biblioteca geral
    form_title: str | None
    ref: AssetRef
    byte_size: int
    source_size: int
    width: int
    height: int
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime

    @property
    def library(self) -> bool:
        return self.form_slug is None

    @property
    def scope(self) -> str:
        """Onde a imagem vale, como a tela escreve."""
        return "Biblioteca geral" if self.form_slug is None else (self.form_title or self.form_slug)


@dataclass(frozen=True)
class Measure:
    """O que se sabe de um arquivo de imagem: o tamanho em bytes, o tipo (`image/png`, `image/jpeg`
    ou `image/webp`) e as dimensões em pixels."""

    size: int
    content_type: str
    width: int
    height: int


def saved_percent(before: int, after: int) -> int | None:
    """O quanto o arquivo encolheu, em porcentagem arredondada; `None` se não encolheu."""
    if after >= before:
        return None
    return round((before - after) * 100 / before)


@dataclass(frozen=True)
class Optimization:
    """O que a otimização fez ao arquivo enviado: como ele era (`before`) e como ficou (`after`).
    São números, e não frase: a tela os escreve em português (`app.admin.asset_pages`), e a API os
    devolve como estão."""

    before: Measure
    after: Measure

    @classmethod
    def of(cls, image: OptimizedImage) -> Optimization:
        return cls(
            before=Measure(
                image.source_size, image.source_type, image.source_width, image.source_height
            ),
            after=Measure(image.size, image.content_type, image.width, image.height),
        )

    @property
    def saved_percent(self) -> int | None:
        """O quanto o arquivo encolheu (porcentagem); `None`: não encolheu."""
        return saved_percent(self.before.size, self.after.size)


@dataclass(frozen=True)
class Upload:
    """O que um envio ou uma troca fez: a imagem como ficou e a otimização (os números do arquivo
    enviado e do guardado). `changed` falso: a troca trouxe a mesma imagem, e nada mudou."""

    asset: Asset
    image: OptimizedImage
    changed: bool = True

    @property
    def optimization(self) -> Optimization:
        return Optimization.of(self.image)


def _select() -> sa.Select[Any]:
    return sa.select(FormAsset, Form.slug, Form.title).outerjoin(Form, Form.id == FormAsset.form_id)


def _asset(row: Any) -> Asset:
    asset, slug, title = row
    return Asset(
        id=asset.id,
        name=asset.name,
        form_slug=slug,
        form_title=title,
        ref=AssetRef(asset.sha256, asset.content_type),
        byte_size=asset.byte_size,
        source_size=asset.source_size,
        width=asset.width,
        height=asset.height,
        created_by=asset.created_by,
        created_at=asset.created_at,
        updated_by=asset.updated_by,
        updated_at=asset.updated_at,
    )


def listing(session: Session) -> list[Asset]:
    """Todas as imagens: a biblioteca geral primeiro, depois as de cada formulário, por nome."""
    rows = session.execute(_select().order_by(Form.slug.nulls_first(), FormAsset.name))
    return [_asset(row) for row in rows]


def find(session: Session, asset_id: uuid.UUID) -> Asset | None:
    row = session.execute(_select().where(FormAsset.id == asset_id)).one_or_none()
    return _asset(row) if row is not None else None


def visible(session: Session, slug: str) -> dict[str, Asset]:
    """As imagens que os templates do formulário `slug` enxergam, pelo nome: as da biblioteca e as
    dele, e a dele vale mais que a da biblioteca com o mesmo nome."""
    rows = session.execute(_select().where(sa.or_(FormAsset.form_id.is_(None), Form.slug == slug)))
    found: dict[str, Asset] = {}
    for asset in sorted((_asset(row) for row in rows), key=lambda item: not item.library):
        found[asset.name] = asset
    return found


def uses_of(session: Session, asset_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[Use]]:
    """As versões publicadas que usam cada uma das imagens, por formulário e número, numa leitura só.
    Uma imagem que nenhuma versão usa não entra no mapa."""
    if not asset_ids:
        return {}
    rows = session.execute(
        sa.select(FormVersionAsset.asset_id, Form.slug, FormVersion.number)
        .join(FormVersion, FormVersion.form_id == Form.id)
        .join(FormVersionAsset, FormVersionAsset.form_version_id == FormVersion.id)
        .where(FormVersionAsset.asset_id.in_(asset_ids))
        .order_by(Form.slug, FormVersion.number)
    )
    found: dict[uuid.UUID, list[Use]] = {}
    for asset_id, slug, number in rows:
        found.setdefault(asset_id, []).append(Use(slug, number))
    return found


def uses(session: Session, asset_id: uuid.UUID) -> list[Use]:
    """As versões publicadas que usam a imagem, por formulário e número."""
    return uses_of(session, [asset_id]).get(asset_id, [])


# --- enviar, trocar, remover --------------------------------------------------------------------


def _optimized(content: bytes) -> OptimizedImage:
    if not content:
        raise AssetRefused("file", NO_FILE)
    try:
        return optimize(content)
    except ImageRefused as refused:
        raise AssetRefused("file", str(refused)) from None


def _scope_id(session: Session, slug: str) -> uuid.UUID | None:
    """O id do formulário do escopo; None para a biblioteca geral (`slug` vazio)."""
    if not slug:
        return None
    form_id = session.scalar(sa.select(Form.id).where(Form.slug == slug))
    if form_id is None:
        raise AssetRefused("scope", UNKNOWN_SCOPE)
    return form_id  # type: ignore[no-any-return]


def _taken(session: Session, form_id: uuid.UUID | None, name: str) -> bool:
    scope = FormAsset.form_id.is_(None) if form_id is None else FormAsset.form_id == form_id
    return session.scalar(sa.select(FormAsset.id).where(scope, FormAsset.name == name)) is not None


def _audit(
    action: str, actor: str, asset_id: uuid.UUID, slug: str, name: str, **details: Any
) -> AuditLog:
    return AuditLog(
        actor=actor,
        action=action,
        target_type="asset",
        target_id=str(asset_id),
        details={"form_slug": slug or None, "name": name, **details},
    )


def upload(deps: Deps, *, scope: str, name: str, content: bytes, actor: str) -> Upload:
    """Envia uma imagem nova, em nome de `actor`: otimiza, guarda os bytes no storage e grava a
    linha e a auditoria. `scope` é o slug do formulário, ou vazio para a biblioteca geral. Levanta
    `AssetRefused` (nome, escopo ou arquivo); nada é gravado nesse caso. Uma falha do storage sobe
    como `RetryableError` ou `TerminalError`."""
    name = name.strip()
    problem = name_problem(name)
    if problem is not None:
        raise AssetRefused("name", problem)
    image = _optimized(content)
    with deps.sessions() as session:
        form_id = _scope_id(session, scope)
        if _taken(session, form_id, name):
            raise AssetRefused("name", NAME_TAKEN)
    ref = AssetRef(image.sha256, image.content_type)
    AssetStore(deps.storage).put(ref, image.content)
    asset_id = uuid.uuid4()
    now = utcnow()
    try:
        with deps.sessions.begin() as session:
            session.add(
                FormAsset(
                    id=asset_id,
                    form_id=form_id,
                    name=name,
                    content_type=image.content_type,
                    byte_size=image.size,
                    source_size=image.source_size,
                    width=image.width,
                    height=image.height,
                    sha256=image.sha256,
                    storage_key=ref.key,
                    created_by=actor,
                    created_at=now,
                    updated_by=actor,
                    updated_at=now,
                )
            )
            session.add(
                _audit(
                    "asset.upload",
                    actor,
                    asset_id,
                    scope,
                    name,
                    sha256=image.sha256,
                    bytes=image.size,
                    source_bytes=image.source_size,
                )
            )
    except IntegrityError:  # outra aba pegou o nome no meio do caminho
        raise AssetRefused("name", NAME_TAKEN) from None
    with deps.sessions() as session:
        asset = find(session, asset_id)
    assert asset is not None
    return Upload(asset, image)


def replace(
    deps: Deps, asset_id: uuid.UUID, content: bytes, *, expected: str, actor: str
) -> Upload:
    """Troca o conteúdo da imagem, mantendo o nome, em nome de `actor`. `expected` é o sha256 que a
    página mostrou. As versões publicadas seguem com o conteúdo antigo. Levanta `UnknownAsset`,
    `AssetChanged` ou `AssetRefused` (o arquivo); nesses casos, nada é gravado. A mesma imagem de
    antes não muda nada (`Upload.changed` falso)."""
    image = _optimized(content)
    with deps.sessions() as session:
        asset = find(session, asset_id)
    if asset is None:
        raise UnknownAsset(str(asset_id))
    if asset.ref.sha256 != expected:
        raise AssetChanged
    ref = AssetRef(image.sha256, image.content_type)
    if ref == asset.ref:
        return Upload(asset, image, changed=False)
    AssetStore(deps.storage).put(ref, image.content)
    with deps.sessions.begin() as session:
        done = session.execute(
            sa.update(FormAsset)
            .where(FormAsset.id == asset_id, FormAsset.sha256 == expected)
            .values(
                content_type=image.content_type,
                byte_size=image.size,
                source_size=image.source_size,
                width=image.width,
                height=image.height,
                sha256=image.sha256,
                storage_key=ref.key,
                updated_by=actor,
                updated_at=utcnow(),
            )
            .returning(FormAsset.id)
        ).one_or_none()
        if done is None:  # outra troca passou na frente
            raise AssetChanged
        session.add(
            _audit(
                "asset.replace",
                actor,
                asset_id,
                asset.form_slug or "",
                asset.name,
                sha256=image.sha256,
                previous_sha256=asset.ref.sha256,
                bytes=image.size,
                source_bytes=image.source_size,
            )
        )
    _discard_unused(deps, asset.ref)
    with deps.sessions() as session:
        changed = find(session, asset_id)
    assert changed is not None
    return Upload(changed, image)


def remove(deps: Deps, asset_id: uuid.UUID, *, expected: str, actor: str) -> Asset:
    """Remove a imagem, em nome de `actor`, e devolve a que saiu. `expected` é o sha256 que a página
    mostrou. Levanta `UnknownAsset`, `AssetChanged` ou `AssetInUse` (uma versão publicada a usa);
    nesses casos, nada é gravado."""
    try:
        with deps.sessions.begin() as session:
            asset = find(session, asset_id)
            if asset is None:
                raise UnknownAsset(str(asset_id))
            if asset.ref.sha256 != expected:
                raise AssetChanged
            used = uses(session, asset_id)
            if used:
                raise AssetInUse(used)
            session.execute(sa.delete(FormAsset).where(FormAsset.id == asset_id))
            session.add(
                _audit(
                    "asset.remove",
                    actor,
                    asset_id,
                    asset.form_slug or "",
                    asset.name,
                    sha256=asset.ref.sha256,
                )
            )
    except IntegrityError:  # uma publicação passou a usar a imagem no meio do caminho
        raise AssetInUse(()) from None
    _discard_unused(deps, asset.ref)
    return asset


def _discard_unused(deps: Deps, ref: AssetRef) -> None:
    """Apaga o arquivo do storage se nenhuma imagem nem versão publicada aponta mais para o
    conteúdo. Uma falha do storage não desfaz a troca ou a remoção: o arquivo fica sobrando, sem
    mal, e o log diz só a chave."""
    with deps.sessions() as session:
        in_a_asset = sa.select(FormAsset.id).where(
            FormAsset.sha256 == ref.sha256, FormAsset.content_type == ref.content_type
        )
        in_a_version = sa.select(FormVersionAsset.name).where(
            FormVersionAsset.sha256 == ref.sha256,
            FormVersionAsset.content_type == ref.content_type,
        )
        if session.scalar(in_a_asset.limit(1)) is not None:
            return
        if session.scalar(in_a_version.limit(1)) is not None:
            return
    try:
        AssetStore(deps.storage).discard(ref)
    except Exception:  # noqa: BLE001 - o arquivo sobra, e só isso
        logger.warning("imagem de formulário: o arquivo %s não foi apagado do storage", ref.key)


# --- o que os templates usam --------------------------------------------------------------------


def used_names(document: Mapping[str, Any]) -> list[str]:
    """Os nomes de imagem (`asset:<nome>`) da abertura, PDF, CSS e prévia da definição."""
    pdf = document.get("pdf")
    pdf = pdf if isinstance(pdf, Mapping) else {}
    texts = (pdf.get("html"), pdf.get("css"), document.get("preview"))
    html, css, preview = (text if isinstance(text, str) else "" for text in texts)
    names = asset_names(html, css, preview)
    logo = document.get("opening_logo")
    if isinstance(logo, Mapping) and isinstance(logo.get("src"), str):
        address = logo["src"].strip()
        name = address[6:]
        if address.startswith("asset:") and name_problem(name) is None and name not in names:
            names.append(name)
    return names


def live_refs(session: Session, document: Mapping[str, Any]) -> dict[str, AssetRef]:
    """As imagens que a definição usa, com o conteúdo de agora: o que o rascunho e os testes dele
    mostram. Um nome que não existe fica de fora (a conferência ao salvar já o aponta)."""
    names = used_names(document)
    if not names:
        return {}
    found = visible(session, str(document.get("slug", "")))
    return {name: found[name].ref for name in names if name in found}


def version_refs(session: Session, slug: str, number: int) -> dict[str, AssetRef]:
    """As imagens que a versão publicada fixou, pelo nome."""
    rows = session.execute(
        sa.select(FormVersionAsset.name, FormVersionAsset.sha256, FormVersionAsset.content_type)
        .join(FormVersion, FormVersion.id == FormVersionAsset.form_version_id)
        .join(Form, Form.id == FormVersion.form_id)
        .where(Form.slug == slug, FormVersion.number == number)
    )
    return {name: AssetRef(sha256, content_type) for name, sha256, content_type in rows}


def pin_version(
    session: Session, version_id: uuid.UUID, slug: str, document: Mapping[str, Any]
) -> dict[str, AssetRef]:
    """Fixa, para a versão recém-criada, o conteúdo de cada imagem que a definição usa, na mesma
    transação da publicação. Devolve o que fixou. Levanta `MissingImages` se um nome não existe
    (mais): a publicação inteira desfaz."""
    session.flush()  # a versão tem de existir antes das linhas que apontam para ela
    found = visible(session, slug)
    missing = [name for name in used_names(document) if name not in found]
    if missing:
        raise MissingImages(missing)
    pinned: dict[str, AssetRef] = {}
    for name in used_names(document):
        asset = found[name]
        session.add(
            FormVersionAsset(
                form_version_id=version_id,
                name=name,
                asset_id=asset.id,
                sha256=asset.ref.sha256,
                content_type=asset.ref.content_type,
            )
        )
        pinned[name] = asset.ref
    return pinned


def stale_names(
    session: Session, slug: str, document: Mapping[str, Any], current_number: int | None
) -> tuple[str, ...]:
    """As imagens que a versão atual fixou com um conteúdo e que mudaram desde então, entre as que
    a definição ainda usa: a imagem foi trocada, e os envios novos só a pegam numa versão nova."""
    names = used_names(document)
    if not names or current_number is None:
        return ()
    pinned = version_refs(session, slug, current_number)
    now = live_refs(session, document)
    return tuple(
        name for name in names if name in pinned and name in now and pinned[name] != now[name]
    )


def stale_slugs(session: Session) -> frozenset[str]:
    """Os formulários cuja versão atual fixou uma imagem que foi trocada desde então: a lista do
    admin os marca como rascunho pendente, porque há o que publicar. É a mesma conta de
    `stale_names`, sobre o que a versão atual fixou, sem ler os rascunhos."""
    latest = (
        sa.select(FormVersion.form_id, sa.func.max(FormVersion.number).label("number"))
        .group_by(FormVersion.form_id)
        .subquery()
    )
    rows = session.execute(
        sa.select(Form.slug, FormVersionAsset.name, FormVersionAsset.sha256)
        .join(FormVersion, FormVersion.form_id == Form.id)
        .join(
            latest,
            sa.and_(latest.c.form_id == FormVersion.form_id, latest.c.number == FormVersion.number),
        )
        .join(FormVersionAsset, FormVersionAsset.form_version_id == FormVersion.id)
    ).all()
    pinned: dict[str, dict[str, str]] = {}
    for slug, name, sha256 in rows:
        pinned.setdefault(slug, {})[name] = sha256
    stale = set()
    for slug, pins in pinned.items():
        now = visible(session, slug)
        if any(name in now and now[name].ref.sha256 != sha256 for name, sha256 in pins.items()):
            stale.add(slug)
    return frozenset(stale)


def servable(session: Session, ref: AssetRef) -> bool:
    """A rota pública pode servir este conteúdo: alguma imagem tem esse sha256 e tipo, ou alguma
    versão publicada o fixou (o resultado de um pedido antigo ainda o mostra, mesmo depois da
    troca)."""
    in_a_asset = sa.select(FormAsset.id).where(
        FormAsset.sha256 == ref.sha256, FormAsset.content_type == ref.content_type
    )
    if session.scalar(in_a_asset.limit(1)) is not None:
        return True
    in_a_version = sa.select(FormVersionAsset.name).where(
        FormVersionAsset.sha256 == ref.sha256, FormVersionAsset.content_type == ref.content_type
    )
    return session.scalar(in_a_version.limit(1)) is not None
