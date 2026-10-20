"""As ações do admin sobre os destinos do webhook: achar o escopo (o destino padrão ou o de um
formulário), salvar o endereço, trocar o segredo e remover, e decidir se um conflito é o outro clique
de um clique duplo.

A página e a API chamam as mesmas funções (ADR 0015, "A página e a API dividem os serviços"): elas não
conhecem `Request`, HTML nem aviso de tela. Recebem o autor (`actor`: `ADMIN_USERNAME` pela página,
`chave:<nome>` pela API), devolvem um resultado ou levantam uma exceção de domínio
(`app.destinations`: `InvalidUrl`, `NoCipher`, `Conflict`), e cada lado a traduz. O escopo e a
decisão do clique duplo moravam dentro das rotas HTML; moveram-se para cá sem mudar.

O escopo `None` é o destino padrão, como em `app.destinations`; a palavra `default` é o nome dele na
API (`DEFAULT_SCOPE`). **O segredo só sai do resultado de `save` (na criação) e de `rotate`**, para quem
o mostra uma vez: nada aqui o guarda, o loga nem o põe numa exceção.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import destinations
from app.admin.queries import forms as form_queries
from app.capture_deliveries import Counts
from app.capture_deliveries import counts as capture_counts
from app.deps import Deps

BODY_START_CHARS = 500  # do corpo da resposta ao evento de teste, o que a tela e a API mostram

# A palavra da API para o destino padrão. Nenhum formulário a tem: `default` é endereço reservado.
DEFAULT_SCOPE = "default"


class UnknownScope(Exception):
    """O escopo não existe: o formulário não está no registro e não tem destino guardado."""


@dataclass(frozen=True)
class Target:
    """O escopo de uma ação: `form_slug` None é o destino padrão. `title` é o do formulário, ou None
    no padrão e num formulário que saiu do código mas ainda tem destino guardado."""

    form_slug: str | None
    title: str | None

    @property
    def scope(self) -> str:
        """O escopo como a API o escreve: `default` ou o slug do formulário."""
        return self.form_slug or DEFAULT_SCOPE


def target(session: Session, form_slug: str | None) -> Target:
    """O escopo de `form_slug` (None: o destino padrão). Vale um formulário do registro ou um slug
    que saiu dele mas ainda tem destino guardado (para poder removê-lo); qualquer outro é
    `UnknownScope`."""
    if form_slug is None:
        return Target(form_slug=None, title=None)
    title = form_queries.form_title(session, form_slug)
    if title is not None:
        return Target(form_slug=form_slug, title=title)
    if form_slug in destinations.listing(session):
        return Target(form_slug=form_slug, title=None)
    raise UnknownScope


def target_of_scope(session: Session, scope: str) -> Target:
    """O escopo de uma palavra da API: `default` é o destino padrão, e qualquer outra, o slug de um
    formulário (`target`). Como `default` é endereço reservado, nenhum formulário a tem."""
    return target(session, None if scope == DEFAULT_SCOPE else scope)


def save(
    deps: Deps,
    form_slug: str | None,
    *,
    url: str,
    actor: str,
    expected_version: int,
    http_confirmed: bool,
    events: list[str] | None = None,
) -> destinations.SaveResult:
    """Cria o destino (com o segredo novo em `secret`, que só existe neste resultado) ou muda o
    endereço do que existe, que mantém o segredo. `expected_version` é a versão que a pessoa viu (0:
    ainda não havia destino). Em produção, um endereço http só passa com `http_confirmed`.

    Levanta `InvalidUrl` (`needs_confirmation` quando só falta confirmar o http), `NoCipher` (sem
    `WEBHOOK_ENCRYPTION_KEY` não há onde guardar um segredo novo) e `Conflict` (o destino mudou:
    nada foi gravado)."""
    with deps.sessions.begin() as db:
        result = destinations.save(
            db,
            deps.cipher,
            form_slug=form_slug,
            url=url,
            actor=actor,
            expected_version=expected_version,
            production=deps.settings.app_env == "production",
            http_confirmed=http_confirmed,
            events=events,
        )
        db.flush()
        from app.integration_readiness import webhook_problems

        if issues := webhook_problems(db, form_slug):
            raise destinations.InvalidEvents(issues[0].message)
        return result


@dataclass(frozen=True)
class Rotated:
    """A troca do segredo: o destino como ficou (com a versão nova) e o segredo novo, que só existe
    neste resultado."""

    destination: destinations.StoredDestination
    secret: str = field(repr=False)


def rotate(deps: Deps, form_slug: str | None, *, actor: str, expected_version: int) -> Rotated:
    """Troca o segredo do destino e devolve o novo, que só existe neste resultado, com o destino como
    ficou; o antigo deixa de valer na hora. Levanta `NoCipher` e `Conflict`."""
    with deps.sessions.begin() as db:
        secret = destinations.rotate(
            db, deps.cipher, form_slug=form_slug, actor=actor, expected_version=expected_version
        )
        # A linha está travada por esta transação: ela existe, e já com a versão nova.
        return Rotated(destination=destinations.listing(db)[form_slug], secret=secret)


@dataclass(frozen=True)
class Removed:
    """A remoção: o destino como estava e quem entrega os eventos do escopo agora (`serving`:
    `default` ou `none`, com o `host`)."""

    destination: destinations.StoredDestination
    serving: str
    serving_host: str | None
    capture_counts: Counts = field(default_factory=Counts)


def remove(deps: Deps, form_slug: str | None, *, actor: str, expected_version: int) -> Removed:
    """Apaga o destino: o formulário passa a usar o destino padrão, se houver, e sem ele os planos
    deixam de ser entregues. Devolve o destino como estava e quem entrega agora. Levanta
    `Conflict`."""
    with deps.sessions.begin() as db:
        from app.integration_readiness import configuration_lock

        configuration_lock(db)
        current = destinations.listing(db).get(form_slug)
        counts = capture_counts(db, webhook_destination_id=current.id) if current else Counts()
        removed = destinations.remove(
            db, form_slug=form_slug, actor=actor, expected_version=expected_version
        )
        db.flush()
        from app.integration_readiness import webhook_problems

        if webhook_problems(db, form_slug):
            raise destinations.Conflict()
        remaining = destinations.listing(db)
    serving = remaining.get(None) if form_slug is not None else None
    return Removed(
        destination=removed,
        serving="default" if serving else "none",
        serving_host=serving.host if serving else None,
        capture_counts=counts,
    )


def secret_already_shown(
    conflict: destinations.Conflict, *, actor: str, expected_version: int, makes_secret: bool
) -> bool:
    """O conflito de uma ação que **gera um segredo** (criar o destino, trocar o segredo) é o outro
    clique de um clique duplo (ou um F5 no POST): a mudança que passou na frente é do próprio `actor`,
    logo depois da versão que ele viu. O segredo já existe, mas só apareceu na resposta daquele outro
    clique, e a pessoa precisa saber disso em vez de "nada foi alterado". Uma ação que não gera
    segredo (mudar o endereço, remover) não tem essa leitura."""
    return makes_secret and conflict.by_the_same_click(actor, expected_version)
