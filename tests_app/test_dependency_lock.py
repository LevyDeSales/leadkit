"""Dependências travadas: o `uv.lock` acompanha o `pyproject.toml`, traz hash de tudo que instala,
e a imagem e o CI instalam dele, a partir de imagens e de um uv com versão e digest fixos.

O docker e a rede não estão disponíveis na suíte, então os testes leem os arquivos: o `uv lock
--check` do CI e o build da imagem fazem a conferência de verdade.
"""

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
LOCK = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
TESTS_WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

PINNED_IMAGE = re.compile(r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")


def declared_requirements() -> set[tuple[str, tuple[str, ...], str, str]]:
    """(nome, extras, versões, marcador) de cada dependência do pyproject, como o uv.lock grava."""
    project = PYPROJECT["project"]
    found = set()
    for extra, requirements in (("", project["dependencies"]), *_optional(project)):
        for text in requirements:
            requirement = Requirement(text)
            marker = f"extra == '{extra}'" if extra else ""
            found.add(
                (
                    canonicalize_name(requirement.name),
                    tuple(sorted(requirement.extras)),
                    str(SpecifierSet(str(requirement.specifier))),
                    marker,
                )
            )
    return found


def _optional(project: dict) -> list[tuple[str, list[str]]]:
    return list(project.get("optional-dependencies", {}).items())


def locked_requirements() -> set[tuple[str, tuple[str, ...], str, str]]:
    (root,) = [p for p in LOCK["package"] if p["name"] == PYPROJECT["project"]["name"]]
    return {
        (
            canonicalize_name(item["name"]),
            tuple(sorted(item.get("extras", []))),
            str(SpecifierSet(item.get("specifier", ""))),
            item.get("marker", ""),
        )
        for item in root["metadata"]["requires-dist"]
    }


def test_lockfile_matches_the_dependencies_in_pyproject() -> None:
    """Mudou uma dependência no pyproject sem refazer o lockfile (`uv lock`): o build da imagem
    e o CI, que instalam com `--locked`, recusariam."""
    assert locked_requirements() == declared_requirements()


def test_every_locked_package_comes_from_pypi_with_hashes() -> None:
    """O uv confere o hash de cada arquivo ao instalar: sem hash, ou com um pacote de git ou de
    caminho local, a instalação deixaria de ser reprodutível."""
    registry = "https://pypi.org/simple"
    for package in LOCK["package"]:
        if package["name"] == PYPROJECT["project"]["name"]:
            continue
        assert package["source"] == {"registry": registry}, package["name"]
        files = [*package.get("wheels", []), *([package["sdist"]] if "sdist" in package else [])]
        assert files, package["name"]
        assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", f["hash"]) for f in files), package["name"]


def test_the_images_the_dockerfile_uses_are_pinned_by_digest() -> None:
    references = re.findall(
        r"^(?:FROM|COPY --from=)(?: --platform=\S+)?\s*(\S+)", DOCKERFILE, re.MULTILINE
    )
    references += re.findall(r"--mount=from=(\S+?)[,\s]", DOCKERFILE)
    images = [ref for ref in references if ":" in ref]

    assert any(ref.startswith("python:3.11-slim-trixie@") for ref in images)
    assert any(ref.startswith("ghcr.io/astral-sh/uv:") for ref in images)
    assert [ref for ref in images if not PINNED_IMAGE.match(ref)] == []


def test_the_image_and_the_ci_install_from_the_lockfile() -> None:
    assert "uv sync --locked" in DOCKERFILE
    assert "pip install" not in DOCKERFILE
    assert "uv sync --locked" in TESTS_WORKFLOW
    # O wheel local é a única instalação fora do lock: nunca resolve dependências.
    installs = [line.strip() for line in TESTS_WORKFLOW.splitlines() if "pip install" in line]
    assert installs == [
        (
            'uv pip install --python "$RUNNER_TEMP/wheel-env/bin/python" '
            '--no-deps --no-editable "$RUNNER_TEMP"/wheels/leadkit-*.whl'
        )
    ]
    assert (
        "uv export --locked --extra mcp --no-dev --no-emit-project "
        '--output-file "$RUNNER_TEMP/wheel-requirements.txt"'
    ) in TESTS_WORKFLOW
    assert (
        'uv pip sync --python "$RUNNER_TEMP/wheel-env/bin/python" '
        '"$RUNNER_TEMP/wheel-requirements.txt"'
    ) in TESTS_WORKFLOW


def test_the_docker_build_context_lets_the_lockfile_in() -> None:
    """O .dockerignore é uma lista de permissão: sem `!uv.lock`, o `COPY` do Dockerfile falha."""
    assert "!uv.lock" in DOCKERIGNORE
    assert "!pyproject.toml" in DOCKERIGNORE


def test_the_image_and_the_ci_use_the_same_uv_version() -> None:
    (in_image,) = re.findall(r"ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+)@sha256:", DOCKERFILE)
    (in_ci,) = re.findall(
        r"""^\s+version:\s*["']?(\d+\.\d+\.\d+)["']?\s*$""", TESTS_WORKFLOW, re.MULTILINE
    )

    assert in_image == in_ci


# --- o extra `mcp` (o SDK do MCP de admin, fase 9.1) ------------------------------------------------


def _requirement_names(texts: list[str]) -> set[str]:
    return {canonicalize_name(Requirement(text).name) for text in texts}


def _reachable_from_the_app() -> set[str]:
    """Os pacotes que `uv sync` (sem extras) instala: o fecho das dependências do app no lockfile."""
    packages = {canonicalize_name(p["name"]): p for p in LOCK["package"]}
    root = canonicalize_name(PYPROJECT["project"]["name"])
    pending = [canonicalize_name(d["name"]) for d in packages[root].get("dependencies", [])]
    found: set[str] = set()
    while pending:
        name = pending.pop()
        if name not in found:
            found.add(name)
            pending.extend(
                canonicalize_name(d["name"]) for d in packages[name].get("dependencies", [])
            )
    return found


def test_the_mcp_sdk_is_an_optional_extra_pinned_exactly() -> None:
    """O servidor da API não carrega o SDK: ele vai num extra próprio (`uv sync --extra mcp`, na
    máquina de quem roda o MCP), com versão exata, porque o SDK muda a API entre versões menores."""
    project = PYPROJECT["project"]
    extras = project["optional-dependencies"]

    assert "mcp" not in _requirement_names(project["dependencies"])
    assert "mcp" not in _requirement_names(extras["dev"])
    (sdk,) = [Requirement(text) for text in extras["mcp"]]
    assert canonicalize_name(sdk.name) == "mcp"
    ((operator, version),) = [(s.operator, s.version) for s in sdk.specifier]
    assert operator == "==" and "*" not in version


def test_the_lock_covers_the_mcp_extra_with_the_pinned_version() -> None:
    (root,) = [p for p in LOCK["package"] if p["name"] == PYPROJECT["project"]["name"]]
    (sdk,) = [Requirement(text) for text in PYPROJECT["project"]["optional-dependencies"]["mcp"]]
    (locked,) = [p for p in LOCK["package"] if p["name"] == "mcp"]

    assert root["optional-dependencies"]["mcp"] == [{"name": "mcp"}]
    assert [s.version for s in sdk.specifier] == [locked["version"]]


def test_the_sdk_is_not_installed_by_the_app_or_by_the_image() -> None:
    """Sem extra, o `uv sync` do `Dockerfile` não chega ao SDK nem ao que só ele traz."""
    assert "mcp" not in _reachable_from_the_app()
    assert "--extra" not in DOCKERFILE and "--all-extras" not in DOCKERFILE


def test_the_ci_installs_the_mcp_extra_so_its_tests_run() -> None:
    assert "uv sync --locked --extra dev --extra mcp" in TESTS_WORKFLOW
