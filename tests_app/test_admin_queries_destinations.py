"""A consulta dos destinos do webhook que a página e a API dividem (`app.admin.queries.destinations`):
quem serve cada formulário, os avisos de "sem destino" e de "segredo que não abre" como dados, e o
destino de um escopo. Sem rótulo de tela: a página os decora em `app.admin.views`, e os textos dela
estão nos testes da página (`test_admin_destinations.py`)."""

from __future__ import annotations

from cryptography.fernet import Fernet

from app import destinations
from app.admin.queries import destinations as queries
from app.deps import Deps
from app.destinations import SecretCipher

LEGACY = ["demo", "mcc", "reforma-2027"]


def store(deps: Deps, form_slug: str | None, url: str = "https://hooks.example.test/x") -> None:
    with deps.sessions.begin() as session:
        destinations.save(
            session,
            deps.cipher,
            form_slug=form_slug,
            url=url,
            actor="testes",
            expected_version=0,
            production=False,
            http_confirmed=False,
        )


def overview(deps: Deps, cipher: SecretCipher | None = None) -> queries.Overview:
    with deps.sessions() as session:
        return queries.overview(session, deps.forms.current_forms(), cipher or deps.cipher)


def detail(
    deps: Deps, form_slug: str | None, title: str | None = None, cipher: SecretCipher | None = None
) -> queries.Detail:
    with deps.sessions() as session:
        return queries.detail(session, form_slug, title, cipher or deps.cipher)


def test_with_no_destination_every_form_is_unserved_and_nothing_is_labelled(deps: Deps) -> None:
    found = overview(deps)

    assert found.default is None and found.unreadable == []
    assert [row.slug for row in found.forms] == LEGACY == found.unserved
    assert {(row.serving, row.host, row.version) for row in found.forms} == {("none", None, 0)}
    assert all(row.title for row in found.forms)  # o título do registro, e não um rótulo de tela


def test_the_default_destination_serves_the_forms_that_have_none_of_their_own(deps: Deps) -> None:
    store(deps, None, "https://padrao.example.test:8443/caminho")
    store(deps, "mcc", "https://mcc.example.test/hook")

    found = overview(deps)

    assert found.default is not None and found.default.host == "padrao.example.test:8443"
    rows = {row.slug: row for row in found.forms}
    assert (rows["mcc"].serving, rows["mcc"].host, rows["mcc"].version) == (
        "form",
        "mcc.example.test",
        1,
    )
    for slug in ("demo", "reforma-2027"):
        assert (rows[slug].serving, rows[slug].host) == ("default", "padrao.example.test:8443")
        assert rows[slug].version == 0  # o destino padrão não é dele
    assert found.unserved == []


def test_a_destination_kept_for_a_form_that_left_the_code_is_listed_without_a_title(
    deps: Deps,
) -> None:
    store(deps, "formulario-antigo")

    found = overview(deps)

    assert [row.slug for row in found.forms] == [*LEGACY, "formulario-antigo"]
    old = found.forms[-1]
    assert (old.title, old.serving, old.version) == (None, "form", 1)


def test_the_destinations_whose_secret_does_not_open_are_named_by_scope(deps: Deps) -> None:
    store(deps, None)
    store(deps, "mcc")
    other = SecretCipher((Fernet.generate_key().decode(),))

    assert overview(deps).unreadable == []
    assert sorted(overview(deps, other).unreadable, key=str) == [None, "mcc"]  # None: o padrão


def test_the_detail_of_the_default_scope_is_itself_or_nothing(deps: Deps) -> None:
    missing = detail(deps, None)
    assert (missing.stored, missing.serving, missing.serving_host) == (None, "none", None)

    store(deps, None, "https://padrao.example.test/x")
    found = detail(deps, None)

    assert found.stored is not None and found.stored.version == 1
    assert (found.serving, found.serving_host, found.unreadable) == (
        "default",
        "padrao.example.test",
        False,
    )


def test_the_detail_of_a_form_says_which_destination_serves_it(deps: Deps) -> None:
    assert detail(deps, "mcc", "Título").serving == "none"

    store(deps, None, "https://padrao.example.test/x")
    by_default = detail(deps, "mcc", "Título")
    assert by_default.stored is None and by_default.form_title == "Título"
    assert (by_default.serving, by_default.serving_host) == ("default", "padrao.example.test")

    store(deps, "mcc", "https://mcc.example.test/x")
    own = detail(deps, "mcc", "Título")
    assert own.stored is not None and (own.serving, own.serving_host) == (
        "form",
        "mcc.example.test",
    )


def test_the_detail_flags_an_own_secret_that_does_not_open_but_not_the_default_one(
    deps: Deps,
) -> None:
    store(deps, None)
    other = SecretCipher((Fernet.generate_key().decode(),))

    assert detail(deps, None, cipher=other).unreadable is True
    assert detail(deps, "mcc", cipher=other).unreadable is False  # o dele não existe
    store(deps, "mcc")
    assert detail(deps, "mcc", cipher=other).unreadable is True
    assert detail(deps, "mcc").unreadable is False
