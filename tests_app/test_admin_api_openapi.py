"""O `docs/api/openapi.json` (ADR 0015, "O arquivo `openapi.json`"): o OpenAPI da API de admin,
versionado no repositório e conferido contra o que a sub-aplicação gera.

Mudar a API sem atualizar o arquivo reprova aqui, com o diff e o comando que o atualiza; o diff do PR
vira o histórico da API (subir o FastAPI ou o Pydantic também aparece nele). O arquivo é o mesmo em
qualquer máquina: sem dado de ambiente, e com o `servers` relativo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.admin.api import openapi

ROOT = Path(__file__).resolve().parents[1]
COMMAND = "python -m app.admin.api.openapi --write"


def test_the_versioned_file_is_the_one_the_api_generates() -> None:
    problem = openapi.check()

    assert problem is None, problem


def test_the_file_lives_in_docs_api_and_is_json_with_sorted_keys_two_spaces_and_a_final_line() -> (
    None
):
    assert openapi.PATH == ROOT / "docs" / "api" / "openapi.json"
    text = openapi.PATH.read_text(encoding="utf-8")
    document = json.loads(text)

    assert text == json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert text.endswith("}\n") and not text.endswith("\n\n")


def test_the_servers_are_fixed_and_relative_so_the_file_is_the_same_on_every_machine() -> None:
    document = openapi.build()
    text = openapi.render(document)

    assert document["servers"] == [{"url": "/admin/api/v1"}]
    for environment in (str(ROOT), "testserver", "127.0.0.1", "localhost"):
        assert environment not in text, environment


def test_the_contract_version_in_the_file_counts_the_operations() -> None:
    document = openapi.build()
    operations = sum(len(methods) for methods in document["paths"].values())

    assert document["info"]["version"] == f"1.{operations}.0"


def test_generating_twice_gives_the_same_text() -> None:
    assert openapi.render(openapi.build()) == openapi.render(openapi.build())


def operation_count() -> int:
    return sum(len(methods) for methods in openapi.build()["paths"].values())


# --- a falha mostra o diff e o comando ----------------------------------------------------------


@pytest.fixture
def stale(tmp_path: Path) -> Path:
    """Uma cópia do arquivo de uma API que ainda não tinha uma das operações."""
    document = openapi.build()
    del document["paths"]["/me"]
    document["info"]["version"] = f"1.{operation_count() - 1}.0"
    path = tmp_path / "openapi.json"
    path.write_text(openapi.render(document), encoding="utf-8")
    return path


def test_a_stale_file_fails_showing_the_diff_and_the_command_that_updates_it(stale: Path) -> None:
    problem = openapi.check(stale)

    assert problem is not None
    assert COMMAND in problem
    assert "docs/api/openapi.json" in problem
    count = operation_count()
    assert f'-    "version": "1.{count - 1}.0"' in problem
    assert f'+    "version": "1.{count}.0"' in problem
    assert "get_current_api_key" in problem  # a operação que o arquivo velho não tem


def test_a_missing_file_fails_with_the_command(tmp_path: Path) -> None:
    problem = openapi.check(tmp_path / "nao-existe.json")

    assert problem is not None and COMMAND in problem


def test_the_diff_of_a_big_change_is_cut_but_still_says_the_command(tmp_path: Path) -> None:
    path = tmp_path / "openapi.json"
    path.write_text("{}\n", encoding="utf-8")

    problem = openapi.check(path)

    assert problem is not None and COMMAND in problem
    assert len(problem.splitlines()) < openapi.MAX_DIFF_LINES + 10
    assert "linhas a mais" in problem


# --- o comando ----------------------------------------------------------------------------------


def test_without_an_argument_it_only_checks_and_exits_with_an_error_when_it_differs(
    stale: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = stale.read_text(encoding="utf-8")

    assert openapi.main([], path=stale) == 1

    assert COMMAND in capsys.readouterr().err
    assert stale.read_text(encoding="utf-8") == before  # conferir não escreve


def test_check_is_the_same_as_no_argument(stale: Path) -> None:
    assert openapi.main(["--check"], path=stale) == 1


def test_write_updates_the_file_and_then_the_check_passes(
    stale: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert openapi.main(["--write"], path=stale) == 0
    assert "atualizado" in capsys.readouterr().out

    assert stale.read_text(encoding="utf-8") == openapi.render(openapi.build())
    assert openapi.main([], path=stale) == 0


def test_write_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "novo" / "openapi.json"

    assert openapi.main(["--write"], path=path) == 0
    assert openapi.main([], path=path) == 0


def test_write_and_check_do_not_go_together() -> None:
    with pytest.raises(SystemExit):
        openapi.main(["--write", "--check"])


@pytest.mark.parametrize("hash_seed", ["0", "12345"])
def test_the_module_runs_with_no_environment_and_gives_the_same_file_whatever_the_hash_seed(
    hash_seed: str,
) -> None:
    """Como o CI de outra máquina o roda: sem `DATABASE_URL`, sem `.env`, com outra semente de hash."""
    kept = ("PATH", "HOME", "SYSTEMROOT", "DYLD_FALLBACK_LIBRARY_PATH", "LD_LIBRARY_PATH")
    env = {name: value for name, value in os.environ.items() if name in kept}
    env["PYTHONHASHSEED"] = hash_seed

    done = subprocess.run(
        [sys.executable, "-m", "app.admin.api.openapi"],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr
