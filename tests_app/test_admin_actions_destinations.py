"""As ações do admin sobre os destinos do webhook (`app.admin.actions.destinations`): o escopo (o padrão,
um formulário do registro ou um que saiu dele mas ainda tem destino), salvar, trocar o segredo e
remover com a regra de produção e a transação, e a decisão do clique duplo. É o que as páginas
`/admin/destinations/...` e a API (`/destinations/{scope}`) dividem; as páginas têm os testes próprios
(`test_admin_destinations.py`)."""

from __future__ import annotations

import pytest

from app import destinations
from app.admin.actions import destinations as actions
from app.deps import Deps
from app.settings import Settings

URL = "https://hooks.example.test/planos"


def save(
    deps: Deps,
    form_slug: str | None = None,
    *,
    url: str = URL,
    actor: str = "testes",
    expected_version: int = 0,
    http_confirmed: bool = False,
) -> destinations.SaveResult:
    return actions.save(
        deps,
        form_slug,
        url=url,
        purpose="internal",
        actor=actor,
        expected_version=expected_version,
        http_confirmed=http_confirmed,
    )


def stored(deps: Deps) -> dict[str | None, destinations.StoredDestination]:
    with deps.sessions() as session:
        return destinations.listing(session)


def target(deps: Deps, form_slug: str | None) -> actions.Target:
    with deps.sessions() as session:
        return actions.target(session, form_slug)


def scope(deps: Deps, word: str) -> actions.Target:
    with deps.sessions() as session:
        return actions.target_of_scope(session, word)


def test_the_default_scope_needs_no_form_and_a_registry_form_gives_its_title(deps: Deps) -> None:
    assert target(deps, None) == actions.Target(form_slug=None, title=None)
    mcc = target(deps, "mcc")
    assert mcc.form_slug == "mcc" and mcc.title and mcc.scope == "mcc"
    assert target(deps, None).scope == actions.DEFAULT_SCOPE == "default"


def test_a_slug_that_left_the_registry_is_a_scope_only_while_it_keeps_a_destination(
    deps: Deps,
) -> None:
    with pytest.raises(actions.UnknownScope):
        target(deps, "formulario-antigo")

    save(deps, "formulario-antigo")
    old = target(deps, "formulario-antigo")
    assert (old.form_slug, old.title) == ("formulario-antigo", None)

    actions.remove(deps, "formulario-antigo", actor="testes", expected_version=1)
    with pytest.raises(actions.UnknownScope):
        target(deps, "formulario-antigo")


def test_the_word_default_is_the_default_destination_and_any_other_word_is_a_slug(
    deps: Deps,
) -> None:
    assert scope(deps, "default") == actions.Target(form_slug=None, title=None)
    assert scope(deps, "demo").form_slug == "demo"
    with pytest.raises(actions.UnknownScope):
        scope(deps, "nao-existe")


def test_saving_creates_with_a_secret_and_then_changes_the_address_keeping_the_secret(
    deps: Deps,
) -> None:
    created = save(deps, None, actor="primeira")
    assert created.secret and created.destination.version == 1

    changed = save(deps, None, url=URL + "/novo", actor="segunda", expected_version=1)

    assert changed.secret is None and changed.destination.version == 2
    (row,) = stored(deps).values()
    assert (row.url, row.updated_by, row.created_by) == (URL + "/novo", "segunda", "primeira")


def test_in_production_an_http_address_needs_the_confirmation_and_the_audit_keeps_it(
    deps: Deps,
) -> None:
    deps.settings = Settings(_env_file=None, APP_ENV="production")  # type: ignore[call-arg]

    with pytest.raises(destinations.InvalidUrl) as refused:
        save(deps, url="http://hooks.example.test/planos")
    assert refused.value.needs_confirmation and stored(deps) == {}

    save(deps, url="http://hooks.example.test/planos", http_confirmed=True)
    assert stored(deps)[None].url == "http://hooks.example.test/planos"


def test_outside_production_an_http_address_needs_nothing(deps: Deps) -> None:
    assert save(deps, url="http://hooks.example.test/planos").secret


def test_a_stale_version_raises_a_conflict_that_says_how_the_destination_is_now(
    deps: Deps,
) -> None:
    save(deps)

    with pytest.raises(destinations.Conflict) as stale:
        save(deps, expected_version=5)
    with pytest.raises(destinations.Conflict) as gone:
        actions.rotate(deps, "mcc", actor="testes", expected_version=1)

    assert stale.value.current is not None and stale.value.current.version == 1
    assert gone.value.current is None  # o destino do mcc não existe


def test_rotating_returns_a_new_secret_and_removing_deletes_the_destination(deps: Deps) -> None:
    first = save(deps, actor="primeira").secret
    assert first

    rotated = actions.rotate(deps, None, actor="segunda", expected_version=1)

    assert rotated.secret and rotated.secret != first and stored(deps)[None].version == 2
    assert rotated.destination.version == 2 and rotated.secret not in repr(rotated)
    removed = actions.remove(deps, None, actor="terceira", expected_version=2)
    assert stored(deps) == {}
    # o destino sai como estava: a versão e a autoria da última mudança, e não as da remoção
    assert (removed.destination.version, removed.destination.updated_by) == (2, "segunda")
    assert removed.destination.created_by == "primeira" and removed.destination.url == URL


def test_removing_says_who_delivers_the_scope_afterwards(deps: Deps) -> None:
    save(deps, None, url="https://padrao.example.test/x")
    save(deps, "mcc", url="https://mcc.example.test/x")

    own = actions.remove(deps, "mcc", actor="testes", expected_version=1)
    default = actions.remove(deps, None, actor="testes", expected_version=1)

    assert (own.serving, own.serving_host) == ("default", "padrao.example.test")
    assert (default.serving, default.serving_host) == ("none", None)


def test_removing_with_a_stale_version_raises_a_conflict_and_removes_nothing(deps: Deps) -> None:
    save(deps)

    with pytest.raises(destinations.Conflict):
        actions.remove(deps, None, actor="testes", expected_version=7)

    assert stored(deps)[None].version == 1


@pytest.mark.parametrize(
    ("actor", "expected", "makes_secret", "same"),
    [
        ("quem-clicou", 0, True, True),  # o outro clique de uma criação
        ("quem-clicou", 0, False, False),  # mudar o endereço não gera segredo
        ("outra-pessoa", 0, True, False),  # foi outra pessoa quem passou na frente
        ("quem-clicou", 3, True, False),  # a versão não é a logo depois da que ele viu
    ],
)
def test_the_secret_already_shown_is_the_same_author_right_after_the_version_they_saw(
    deps: Deps, actor: str, expected: int, makes_secret: bool, same: bool
) -> None:
    save(deps, actor="quem-clicou")  # a versão 1, criada por quem clicou
    with pytest.raises(destinations.Conflict) as conflict:
        save(deps, expected_version=expected)

    assert (
        actions.secret_already_shown(
            conflict.value, actor=actor, expected_version=expected, makes_secret=makes_secret
        )
        is same
    )


def test_a_conflict_over_a_destination_that_does_not_exist_never_comes_from_the_same_click(
    deps: Deps,
) -> None:
    with pytest.raises(destinations.Conflict) as conflict:
        actions.rotate(deps, None, actor="quem-clicou", expected_version=1)

    assert not actions.secret_already_shown(
        conflict.value, actor="quem-clicou", expected_version=1, makes_secret=True
    )
