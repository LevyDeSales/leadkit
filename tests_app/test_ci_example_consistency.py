"""Executa a preparação real do CI contra modelos sintéticos equivalentes aos exemplos públicos."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = Path(os.environ.get("LEADKIT_CI_EXAMPLES_DIR", ROOT / "deploy"))
IMAGE_STEP = "Write a dummy deploy/.env from the example"
SWARM_STEP = "Write the config folder like the server's"
SECOND_STACK_STEP = "Start a second stack next to the first, with its own name and domain"


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/deploy-image.yml").read_text())


def step_script(job: str, name: str) -> str:
    return next(
        step["run"] for step in workflow()["jobs"][job]["steps"] if step.get("name") == name
    )


def heredoc(script: str) -> str:
    return re.search(r"<<'PY'\n(.*?)\nPY(?:\n|$)", script, re.DOTALL)[1]


def values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def replace_setting(path: Path, key: str, value: str) -> None:
    text, count = re.subn(
        rf"^{key}=.*$", lambda _: f"{key}={value}", path.read_text(), flags=re.MULTILINE
    )
    assert count == 1, key
    path.write_text(text)


def copy_examples(root: Path, user: str, database: str) -> None:
    (root / "deploy/swarm").mkdir(parents=True)
    for name in (
        ".env.example",
        "swarm/app.env.example",
        "swarm/db.env.example",
        "swarm/stack.yaml",
    ):
        shutil.copy2(EXAMPLES / name, root / "deploy" / name)
    for name in (".env.example", "swarm/db.env.example"):
        path = root / "deploy" / name
        replace_setting(path, "POSTGRES_USER", user)
        replace_setting(path, "POSTGRES_DB", database)
    # O padrão do modelo permanece independente dos nomes internos escolhidos para os ensaios.
    replace_setting(root / "deploy/swarm/app.env.example", "STACK_NAME", "leadkit")


@pytest.mark.parametrize(
    "user,database", [("leadkit", "leadkit"), ("ci_role", "ci_database"), ("mcc", "mcc")]
)
def test_compose_preparation_uses_the_database_identity_from_the_example(tmp_path, user, database):
    copy_examples(tmp_path, user, database)
    result = subprocess.run(
        [sys.executable, "-c", heredoc(step_script("image", IMAGE_STEP))],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    generated = values(tmp_path / "deploy/.env")
    url = urlsplit(generated["DATABASE_URL"])
    assert (
        (unquote(url.username), unquote(url.path.removeprefix("/")))
        == (generated["POSTGRES_USER"], generated["POSTGRES_DB"])
        == (user, database)
    )
    assert url.hostname == "postgres" and url.port == 5432
    password_matches = unquote(url.password) == generated["POSTGRES_PASSWORD"]
    assert password_matches


def test_swarm_post_preparation_accepts_the_public_example_stack_name(tmp_path):
    copy_examples(tmp_path, "leadkit", "leadkit")
    configuration = tmp_path / "config"
    configuration.mkdir()
    shutil.copy2(tmp_path / "deploy/swarm/app.env.example", configuration / "app.env")
    shutil.copy2(tmp_path / "deploy/swarm/db.env.example", configuration / "db.env")
    replace_setting(configuration / "app.env", "STACK_NAME", "gerador-planos")
    tail = step_script("swarm", SWARM_STEP).split("\nPY\n", 1)[1]
    result = subprocess.run(
        ["bash", "-e", "-c", tail],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        env={**os.environ, "conf": str(configuration), "CI_STACK_NAME": "gerador-planos"},
    )
    assert result.returncode == 0
    assert values(configuration / "app.env")["STACK_NAME"] == "gerador-planos"
    assert values(tmp_path / "deploy/swarm/app.env.example")["STACK_NAME"] == "leadkit"


def prepare_swarm_config(root: Path) -> Path:
    configuration, output = root / "config", root / "output"
    configuration.mkdir()
    output.mkdir()
    (output / "hatchet-token").write_text("synthetic-ci-token")
    job = workflow()["jobs"]["swarm"]
    environment = {
        **os.environ,
        "DOMAIN": job["env"]["DOMAIN"],
        "CI_STACK_NAME": job["env"]["CI_STACK_NAME"],
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            heredoc(step_script("swarm", SWARM_STEP)),
            str(root / "deploy/swarm"),
            str(configuration),
            str(output),
        ],
        capture_output=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0
    return configuration


@pytest.mark.parametrize(
    "user,database", [("leadkit", "leadkit"), ("ci_role", "ci_database"), ("mcc", "mcc")]
)
def test_both_swarm_configurations_match_their_postgres_and_explicit_stack(
    tmp_path, user, database
):
    copy_examples(tmp_path, user, database)
    first = prepare_swarm_config(tmp_path)
    second = tmp_path / "second-config"
    second.mkdir()
    for name in ("app.env", "db.env"):
        shutil.copy2(first / name, second / name)
    before = [
        hashlib.sha256((first / name).read_bytes()).hexdigest() for name in ("app.env", "db.env")
    ]
    second_domain = workflow()["jobs"]["swarm"]["env"]["DOMAIN_B"]
    second_stack = "gerador-planos-b"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            heredoc(step_script("swarm", SECOND_STACK_STEP)),
            str(second / "app.env"),
            second_domain,
            second_stack,
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    for directory, stack in ((first, "gerador-planos"), (second, second_stack)):
        app, postgres = values(directory / "app.env"), values(directory / "db.env")
        url = urlsplit(app["DATABASE_URL"])
        assert (
            (unquote(url.username), unquote(url.path.removeprefix("/")))
            == (postgres["POSTGRES_USER"], postgres["POSTGRES_DB"])
            == (user, database)
        )
        assert url.hostname == f"{stack}_db" and url.port == 5432
        assert app["STACK_NAME"] == stack
        password_matches = unquote(url.password) == postgres["POSTGRES_PASSWORD"]
        assert password_matches
        assert (
            sum(
                line.startswith("STACK_NAME=")
                for line in (directory / "app.env").read_text().splitlines()
            )
            == 1
        )
    assert values(second / "app.env")["PUBLIC_BASE_URL"] == f"https://{second_domain}"
    after = [
        hashlib.sha256((first / name).read_bytes()).hexdigest() for name in ("app.env", "db.env")
    ]
    assert before == after


@pytest.mark.parametrize(
    "inherited_stack", [None, "", "outer-stack"], ids=["unset", "empty", "inherited"]
)
def test_real_swarm_names_distinguish_default_from_explicit_example(
    tmp_path: Path, inherited_stack: str | None
) -> None:
    """Executa o shell real sobre stack.yaml e os três modelos distribuídos, sem Docker local."""
    copy_examples(tmp_path, "leadkit", "leadkit")
    configuration = tmp_path / "deploy" / "swarm"
    for model, output in (("app.env.example", "app.env"), ("db.env.example", "db.env")):
        shutil.copy2(configuration / model, configuration / output)
    runner = tmp_path / "runner"
    runner.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        "#!"
        + sys.executable
        + "\n"
        + r"""
import os, re, sys
from pathlib import Path
import yaml
assert sys.argv[1:] == ["stack", "config", "-c", "stack.yaml"]
stack = yaml.safe_load(Path("stack.yaml").read_text())
pattern = re.compile(r"\$\{(\w+)(?:(:-|:\?)([^}]*))?\}")
def replace(match):
    name, operator, default = match.groups()
    value = os.environ.get(name, "")
    if value:
        return value
    if operator == ":-":
        return default
    if operator == ":?":
        raise RuntimeError("required interpolation value missing")
    return ""
for label in stack["services"]["api"]["deploy"]["labels"]:
    print(pattern.sub(replace, label))
"""
    )
    docker.chmod(0o700)
    environment = {
        **os.environ,
        "CONF": str(configuration),
        "RUNNER_TEMP": str(runner),
        "GITHUB_WORKSPACE": str(tmp_path),
        "DOMAIN": "synthetic.ci.test",
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
    }
    environment.pop("STACK", None)
    if inherited_stack is not None:
        environment["STACK"] = inherited_stack
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-o",
            "pipefail",
            "-c",
            step_script("swarm", "Check that two stack names never share a Traefik name"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    def names(which: str) -> set[str]:
        return set((runner / ("names-" + which)).read_text().splitlines())

    assert names("empty") == names("unset") == names("default")
    assert "traefik.http.routers.gerador-planos" in names("default")
    assert "traefik.http.routers.leadkit" in names("template")
    assert names("default").isdisjoint(names("template"))
    assert len(names("a")) == len(names("b")) == 6 and names("a").isdisjoint(names("b"))
