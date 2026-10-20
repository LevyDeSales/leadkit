"""A consulta dos destinos do webhook, a que a página (`app.admin.views`) e a API de admin dividem: quem
serve cada formulário, os avisos "sem destino" e "segredo que não abre" e o destino de um escopo.

Devolve dados, não telas: o título do formulário como o registro o tem, o destino que serve (`form`,
`default` ou `none`), o host e a versão, e nenhum rótulo. A página os decora (o selo, a frase, a data
de Brasília) e a API os serializa em JSON. **Nunca o segredo**: a consulta só diz se o guardado abre.
O escopo `None` é o destino padrão, como em `app.destinations`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import destinations
from app.admin.queries.forms import serving_scope
from app.capture_deliveries import Counts
from app.capture_deliveries import counts as capture_counts
from app.forms import FormSpec


@dataclass(frozen=True)
class FormRow:
    """Um formulário e o destino que o serve.

    - `title`: o título no registro; None se o formulário saiu do código mas ainda tem destino
      guardado (para poder removê-lo).
    - `serving`: `form` (o destino é dele), `default` (usa o padrão) ou `none` (os planos dele não são
      entregues); `host` é o do destino que serve, ou None em `none`.
    - `version`: a versão do destino próprio; 0 se ele não tem."""

    slug: str
    title: str | None
    serving: str
    host: str | None
    version: int


@dataclass(frozen=True)
class Overview:
    """Todos os destinos.

    - `default`: o destino padrão, ou None se não há.
    - `forms`: um por formulário do registro e um por slug que saiu dele mas ainda tem destino.
    - `unserved`: os slugs sem destino nenhum, nem o próprio nem o padrão.
    - `unreadable`: os destinos cujo segredo a chave atual não abre (o slug; None é o padrão)."""

    default: destinations.StoredDestination | None
    forms: list[FormRow]
    unserved: list[str]
    unreadable: list[str | None]


def overview(
    session: Session, forms: Iterable[FormSpec], cipher: destinations.SecretCipher | None
) -> Overview:
    """O destino de cada formulário. Um destino guardado para um slug que saiu do código também
    aparece, para poder ser removido."""
    stored = destinations.listing(session)
    titles: dict[str, str | None] = {spec.slug: spec.title for spec in forms}
    titles |= {slug: None for slug in stored if slug is not None and slug not in titles}
    rows = []
    for slug, title in titles.items():
        scope, serving = serving_scope(stored, slug)
        own = stored.get(slug)
        rows.append(
            FormRow(
                slug=slug,
                title=title,
                serving=scope,
                host=serving.host if serving else None,
                version=own.version if own else 0,
            )
        )
    return Overview(
        default=stored.get(None),
        forms=rows,
        unserved=[row.slug for row in rows if row.host is None],
        unreadable=destinations.unreadable(session, cipher),
    )


@dataclass(frozen=True)
class Detail:
    """O destino de um escopo: o padrão (`form_slug` None) ou o de um formulário.

    - `form_title`: o título do formulário; None no padrão e no de um formulário que saiu do código.
    - `stored`: o destino deste escopo; None se ele ainda não tem.
    - `serving` e `serving_host`: quem entrega os eventos deste escopo. No padrão, ele mesmo
      (`default`) ou `none`; num formulário, o próprio (`form`), o padrão (`default`) ou `none`.
    - `unreadable`: o segredo do destino deste escopo não abre com a chave atual."""

    form_slug: str | None
    form_title: str | None
    stored: destinations.StoredDestination | None
    serving: str
    serving_host: str | None
    unreadable: bool
    serving_events: tuple[str, ...] = ()
    capture_counts: Counts = field(default_factory=Counts)


def detail(
    session: Session,
    form_slug: str | None,
    form_title: str | None,
    cipher: destinations.SecretCipher | None,
) -> Detail:
    """O destino do escopo. Quem chama já conferiu que o escopo existe (o formulário está no
    registro ou tem destino guardado): esta consulta só o descreve."""
    stored = destinations.listing(session)
    own = stored.get(form_slug)
    if form_slug is None:
        scope, serving = ("default", own) if own else ("none", None)
    else:
        scope, serving = serving_scope(stored, form_slug)
    return Detail(
        form_slug=form_slug,
        form_title=form_title,
        stored=own,
        serving=scope,
        serving_host=serving.host if serving else None,
        serving_events=serving.events if serving else (),
        capture_counts=capture_counts(session, webhook_destination_id=own.id) if own else Counts(),
        unreadable=own is not None and form_slug in destinations.unreadable(session, cipher),
    )
