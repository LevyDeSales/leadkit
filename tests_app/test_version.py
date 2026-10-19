"""A versão do sistema (`vAAAA.M.N`, ano e mês no fuso de São Paulo): a próxima versão a partir das
tags do repositório, o que o CI imprime para o build e o rodapé do admin.

O CI não roda aqui, então o cálculo é testado como função e pela linha de comando que o workflow
chama (`python -m app.version`), com a saída de `git ls-remote --tags` no lugar do remoto.
"""

from __future__ import annotations

import io
import re
import sys
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from test_admin import PASSWORD_HASH, FakeClock, login

from app import version
from app.api.main import create_app
from app.deps import Deps
from app.settings import Settings

OCTOBER_17 = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)  # 12:00 em São Paulo


def test_the_first_version_of_a_month_is_number_one() -> None:
    assert version.next_version([], OCTOBER_17) == "2026.10.1"
    assert version.next_version(["v2026.9.33", "v2025.10.4"], OCTOBER_17) == "2026.10.1"


def test_the_next_version_follows_the_highest_number_of_the_month() -> None:
    tags = ["v2026.10.1", "v2026.10.2", "v2026.9.40"]
    assert version.next_version(tags, OCTOBER_17) == "2026.10.3"
    # A ordem das tags não importa, e o número é comparado como número ("10" vem depois de "9").
    assert version.next_version(["v2026.10.9", "v2026.10.10", "v2026.10.2"], OCTOBER_17) == (
        "2026.10.11"
    )


def test_tags_that_are_not_versions_of_this_scheme_are_ignored() -> None:
    tags = ["v2026.10.1", "v2026.10.7-beta.1", "v2026.10.07", "v2026.10", "v1.0.0", "latest", ""]
    assert version.next_version(tags, OCTOBER_17) == "2026.10.2"


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # 31/10 22:30 em São Paulo já é 01/11 01:30 em UTC: ainda é outubro.
        (datetime(2026, 11, 1, 1, 30, tzinfo=UTC), "2026.10.4"),
        # 01/10 00:00 UTC ainda é 30/09 21:00 em São Paulo: é setembro.
        (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), "2026.9.2"),
        # 01/11 03:00 UTC é a meia-noite de São Paulo: novembro, e o número volta a 1.
        (datetime(2026, 11, 1, 3, 0, tzinfo=UTC), "2026.11.1"),
        # A virada do ano segue a mesma regra.
        (datetime(2027, 1, 1, 2, 59, tzinfo=UTC), "2026.12.1"),
        (datetime(2027, 1, 1, 3, 0, tzinfo=UTC), "2027.1.1"),
    ],
)
def test_the_month_is_the_one_in_sao_paulo_not_in_utc(moment: datetime, expected: str) -> None:
    tags = ["v2026.9.1", "v2026.10.1", "v2026.10.2", "v2026.10.3"]
    assert version.next_version(tags, moment) == expected


def test_a_moment_without_a_time_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="fuso"):
        version.next_version([], datetime(2026, 10, 17, 12, 0))


SHA = "a" * 40
OTHER = "b" * 40
LS_REMOTE = (
    f"{OTHER}\trefs/tags/v2026.10.1\n"
    f"{OTHER}\trefs/tags/v2026.10.2\n"
    f"{'c' * 40}\trefs/tags/unrelated\n"
)


def test_a_commit_that_already_has_a_version_keeps_it_on_a_rerun() -> None:
    """Se o job de publicação roda de novo no mesmo commit, a versão não anda: a imagem refeita leva
    a mesma tag, e não nasce um segundo release para o mesmo código."""
    refs = LS_REMOTE + f"{SHA}\trefs/tags/v2026.10.3\n"
    release = version.plan_release(refs, SHA, OCTOBER_17)
    assert release == version.Release("2026.10.3", date(2026, 10, 17))


def test_a_commit_with_an_annotated_tag_is_found_by_the_peeled_entry() -> None:
    """Uma tag anotada lista o objeto da tag e, na linha seguinte (`^{}`), o commit."""
    refs = LS_REMOTE + f"{'d' * 40}\trefs/tags/v2026.10.3\n{SHA}\trefs/tags/v2026.10.3^{{}}\n"
    assert version.plan_release(refs, SHA, OCTOBER_17).version == "2026.10.3"
    assert version.plan_release(refs, OTHER, OCTOBER_17).version == "2026.10.2"


def test_a_new_commit_gets_the_next_number_and_the_sao_paulo_date() -> None:
    release = version.plan_release(LS_REMOTE, SHA, datetime(2026, 11, 1, 1, 30, tzinfo=UTC))
    # 22:30 de 31/10 em São Paulo.
    assert release == version.Release("2026.10.3", date(2026, 10, 31))


def test_the_command_line_prints_what_the_build_needs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(LS_REMOTE))

    status = version.main(["--sha", SHA, "--now", "2026-10-17T15:00:00+00:00"])

    assert status == 0
    assert capsys.readouterr().out.splitlines() == [
        "APP_VERSION=2026.10.3",
        "APP_BUILD_DATE=2026-10-17",
    ]


def test_the_command_line_refuses_a_moment_without_a_time_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit):
        version.main(["--sha", SHA, "--now", "2026-10-17T12:00:00"])


@pytest.mark.parametrize(
    ("number", "built_on", "expected"),
    [
        ("2026.10.1", "2026-10-17", "versão 2026.10.1 · 17/10/2026"),
        ("2026.10.1", "", "versão 2026.10.1"),
        ("2026.10.1", "17/10/2026", "versão 2026.10.1"),  # data fora do formato do build: ignorada
        ("2026.10.1", None, "versão 2026.10.1"),
        # Sem versão de release (um build de commit), o hash.
        ("a1b2c3d", "2026-10-17", "build a1b2c3d · 17/10/2026"),
        ("a1b2c3d", None, "build a1b2c3d"),
        (None, "2026-10-17", "dev"),
        ("", "", "dev"),
        ("  ", None, "dev"),
    ],
)
def test_the_version_is_shown_in_portuguese_with_a_brazilian_date(
    number: str | None, built_on: str | None, expected: str
) -> None:
    assert version.describe(number, built_on) == expected


FOOTER = re.compile(r'<footer class="footer"[^>]*>(.*?)</footer>', re.DOTALL)


def admin_page(deps: Deps, path: str = "/admin", **env: str) -> str:
    deps.settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ADMIN_USERNAME="operadora",
        ADMIN_PASSWORD_HASH=PASSWORD_HASH,
        ADMIN_SESSION_SECRET="segredo-da-sessao-admin-0123456789abcdef",
        **env,
    )
    client = TestClient(create_app(trigger=None, clock=FakeClock()), follow_redirects=False)
    assert login(client).status_code == 303
    response = client.get(path)
    assert response.status_code == 200
    return response.text


def footer_text(html: str) -> str:
    match = FOOTER.search(html)
    assert match is not None, "o rodapé não apareceu"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()


def test_the_admin_footer_shows_the_version_and_the_build_date(deps: Deps) -> None:
    html = admin_page(deps, APP_VERSION="2026.10.1", APP_BUILD_DATE="2026-10-17")
    assert footer_text(html) == "versão 2026.10.1 · 17/10/2026"


def test_the_admin_footer_shows_dev_when_the_image_has_no_version(deps: Deps) -> None:
    assert footer_text(admin_page(deps)) == "dev"
    # Um ARG vazio no Dockerfile vira uma variável vazia, e vale como ausente.
    assert footer_text(admin_page(deps, APP_VERSION="", APP_BUILD_DATE="")) == "dev"


def test_every_admin_page_has_the_footer(deps: Deps) -> None:
    html = admin_page(deps, "/admin/forms", APP_VERSION="2026.10.1", APP_BUILD_DATE="2026-10-17")
    assert footer_text(html) == "versão 2026.10.1 · 17/10/2026"


def test_the_login_page_does_not_tell_which_version_is_running(deps: Deps) -> None:
    deps.settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ADMIN_USERNAME="operadora",
        ADMIN_PASSWORD_HASH=PASSWORD_HASH,
        ADMIN_SESSION_SECRET="segredo-da-sessao-admin-0123456789abcdef",
        APP_VERSION="2026.10.1",
    )
    client = TestClient(create_app(trigger=None, clock=FakeClock()), follow_redirects=False)
    page = client.get("/admin/login")
    assert page.status_code == 200 and "2026.10.1" not in page.text


def test_empty_repository_requires_explicit_release_bootstrap() -> None:
    with pytest.raises(ValueError, match="bootstrap"):
        version.plan_release("", SHA, OCTOBER_17)
    assert version.plan_release("", SHA, OCTOBER_17, allow_bootstrap=True).version == "2026.10.1"


def test_bootstrap_does_not_accept_malformed_remote_output() -> None:
    with pytest.raises(ValueError):
        version.plan_release(
            "not-a-sha refs/tags/v2026.10.1", SHA, OCTOBER_17, allow_bootstrap=True
        )


def test_cli_bootstrap_is_explicit_and_prints_no_partial_build_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as failed:
        version.main(["--sha", SHA, "--now", "2026-10-17T15:00:00+00:00"])
    assert failed.value.code == 2
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert (
        version.main(["--sha", SHA, "--allow-bootstrap", "--now", "2026-10-17T15:00:00+00:00"]) == 0
    )
    assert "APP_VERSION=2026.10.1" in capsys.readouterr().out
