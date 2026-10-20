"""Gates do candidato público: um SHA, checks conclusivos e nenhuma publicação automática."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from test_deployment_contract import rollout_engine  # noqa: F401 — fixture PostgreSQL compartilhada

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow.get("on", workflow.get(True))


def test_untrusted_jobs_do_not_receive_publication_permissions_or_credentials() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        workflow = load(path.name)
        assert workflow["permissions"] == {"contents": "read"}
        assert "pull_request_target" not in triggers(workflow)
        for job_name, job in workflow["jobs"].items():
            if job_name == "publish":
                assert name_of_publisher(workflow) == "publish"
                continue
            assert "write" not in job.get("permissions", {}).values()
            assert job.get("secrets") != "inherit"
            for step in job.get("steps", []):
                assert not step.get("with", {}).get("push", False)
                script = step.get("run", "")
                assert not re.search(
                    r"(?:docker\s+push|gh\s+release\s+create|git\s+push|docker\s+login)", script
                )
                assert "${{ secrets." not in str(step)
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False


def test_every_change_reports_the_required_test_and_readiness_checks() -> None:
    workflow = load("tests.yml")
    on = triggers(workflow)
    assert on["push"]["branches"] == ["main", "develop"]
    assert "pull_request" in on
    assert not any("paths" in str(on[event]) for event in ["push", "pull_request"])
    assert workflow["jobs"]["app-tests"].get("if") is None
    assert workflow["jobs"]["ci-ready"]["if"] == "always()"
    assert workflow["jobs"]["ci-ready"]["needs"] == ["app-tests", "packaging"]


def test_image_and_swarm_share_the_tested_commit_without_repeating_the_suite() -> None:
    tests = load("tests.yml")
    packaging = tests["jobs"]["packaging"]
    assert packaging["uses"] == "./.github/workflows/deploy-image.yml"
    assert set(triggers(load("deploy-image.yml"))) == {"workflow_call"}
    assert "concurrency" not in load("deploy-image.yml")
    assert "app-tests" not in load("deploy-image.yml")["jobs"]
    plan = tests["jobs"]["release-plan"]
    assert plan["needs"] == "ci-ready"
    assert "workflow_dispatch" in plan["if"]
    script = next(step["run"] for step in plan["steps"] if step.get("name") == "Pick the version")
    assert "--allow-bootstrap" in script and "BOOTSTRAP_RELEASE" in script
    assert script.index("git ls-remote") < script.index("python3 -m app.version")


def run_gate(job: dict[str, Any], results: dict[str, str], *, swarm_required: bool = True) -> int:
    script = "\n".join(step.get("run", "") for step in job["steps"])
    for name, result in results.items():
        script = script.replace("${{ needs." + name + ".result }}", result)
    script = re.sub(r"\$\{\{ needs\.[^}]+\}\}", "", script)
    return subprocess.run(
        ["bash", "-e", "-c", script],
        capture_output=True,
        check=False,
        env={**os.environ, "SWARM_REQUIRED": str(swarm_required).lower()},
    ).returncode


@pytest.mark.parametrize("test_result", ["failure", "cancelled", "skipped", ""])
def test_failed_or_absent_app_tests_cannot_prepare_a_release(test_result: str) -> None:
    gate = load("tests.yml")["jobs"]["ci-ready"]
    assert run_gate(gate, {"app-tests": test_result, "packaging": "success"}) != 0


@pytest.mark.parametrize("image_result", ["failure", "cancelled", "skipped", ""])
def test_failed_or_absent_image_validation_cannot_prepare_a_release(image_result: str) -> None:
    gate = load("deploy-image.yml")["jobs"]["validation"]
    assert run_gate(gate, {"changes": "success", "image": image_result, "swarm": "success"}) != 0


def test_successful_test_and_packaging_results_open_the_read_only_gate() -> None:
    assert (
        run_gate(
            load("tests.yml")["jobs"]["ci-ready"], {"app-tests": "success", "packaging": "success"}
        )
        == 0
    )


def test_image_smoke_imports_the_demo_explicitly_and_renders_real_pdfs() -> None:
    text = (WORKFLOWS / "deploy-image.yml").read_text()
    assert "legacy_forms" not in text and "/p/mcc" not in text
    assert "examples/demo.json" in text and "import_definition" in text
    assert "WeasyPrintRenderer" in text and "starter.definition" in text
    assert "len(documents) == 2" in text
    assert "python -I" in text


def test_the_wheel_is_built_and_tested_outside_the_source_tree() -> None:
    scripts = "\n".join(s.get("run", "") for s in load("tests.yml")["jobs"]["app-tests"]["steps"])
    assert "uv build --wheel" in scripts
    assert "--no-deps" in scripts and "--no-editable" in scripts
    assert 'python" -I' in scripts
    assert 'cd "$RUNNER_TEMP"' in scripts


def test_the_dockerfile_keeps_version_arguments_and_distribution_notices() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert 'ARG APP_VERSION=""' in text and 'ARG APP_BUILD_DATE=""' in text
    assert "THIRD_PARTY_NOTICES.md" in text and "LICENSE" in text
    assert "--no-editable" in text


NAMES = ("tests.yml", "deploy-image.yml")
BUILD_ACTION = "docker/build-push-action@"
BUILDX_ACTION = "docker/setup-buildx-action@"


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Os padrões de caminho do Actions: `*` não passa de uma pasta, `**` passa."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def test_the_swarm_runs_on_pushes_and_on_pull_requests_that_touch_what_it_tests() -> None:
    jobs = load("deploy-image.yml")["jobs"]
    assert jobs["swarm"]["needs"] == "changes"
    assert jobs["swarm"]["if"] == "needs.changes.outputs.swarm == 'true'"

    output = jobs["changes"]["outputs"]["swarm"]
    # Fora de um PR (push em main), sempre; num PR para main (promoção e hotfix), sempre; nos
    # outros, só se o filtro achar mudança.
    assert "github.event_name != 'pull_request'" in output
    assert "github.base_ref == 'main'" in output
    assert "steps.filter.outputs.swarm == 'true'" in output

    (filter_step,) = [s for s in jobs["changes"]["steps"] if s.get("id") == "filter"]
    assert filter_step["if"] == "github.event_name == 'pull_request'"
    patterns = yaml.safe_load(filter_step["with"]["filters"])["swarm"]
    regexes = [glob_regex(pattern) for pattern in patterns]
    for path in (
        "deploy/swarm/stack.yaml",
        "deploy/swarm/deploy.sh",
        "deploy/swarm/rollout.py",
        "deploy/entrypoint.sh",
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/deploy-image.yml",
        "app/migrations/versions/0008_legacy_forms_as_version_one.py",
        "app/settings.py",
        "app/deployment_contract.py",
        "app/worker.py",
        "app/api/main.py",
    ):
        assert any(regex.match(path) for regex in regexes), path
    # O que só os Tests cobrem não dispara o job mais caro.
    for path in ("app/admin/views.py", "app/forms/definition/__init__.py", "tests_app/conftest.py"):
        assert not any(regex.match(path) for regex in regexes), path


def test_third_party_actions_are_pinned_to_an_exact_version() -> None:
    """Sem tag flutuante (`@v3`): a action só muda quando alguém a troca (CONTRIBUTING.md,
    "Dependências"). As da própria GitHub (`actions/`) seguem a tag maior."""
    floating = []
    for name in NAMES:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        for use in re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.MULTILINE):
            owner, _, ref = use.partition("@")
            if owner.startswith(("actions/", "./")):
                continue
            if not re.fullmatch(r"[0-9a-f]{40}|v\d+\.\d+\.\d+", ref):
                floating.append(use)
    assert floating == []


def steps_of(job: str) -> list[dict[str, Any]]:
    return load("deploy-image.yml")["jobs"][job]["steps"]  # type: ignore[no-any-return]


def uses_of(step: dict[str, Any]) -> str:
    return str(step.get("uses", ""))


@pytest.mark.parametrize("job", ["image", "swarm"])
def test_the_validation_builds_reuse_the_docker_layers_from_the_actions_cache(job: str) -> None:
    """A imagem é construída em dois jobs de validação do mesmo commit. O cache de camadas
    (`type=gha`, num escopo só) deixa a base, o apt e as dependências, que mudam pouco, para a
    primeira construção. Um cache que falha não pode reprovar o job (`ignore-error`)."""
    steps = steps_of(job)
    (buildx,) = [s for s in steps if uses_of(s).startswith(BUILDX_ACTION)]
    (build,) = [s for s in steps if uses_of(s).startswith(BUILD_ACTION)]
    # O builder com cache só é usado pelo passo de build: o `docker build` solto de outro passo
    # continua no builder padrão, que enxerga as imagens locais.
    assert buildx["with"]["use"] is False
    assert steps.index(buildx) < steps.index(build)
    options = build["with"]
    assert options["builder"] == f"${{{{ steps.{buildx['id']}.outputs.name }}}}"
    assert options["context"] == "."
    assert options["load"] is True
    assert options["provenance"] is False
    assert options["cache-from"] == "type=gha,scope=leadkit"
    assert options["cache-to"] == "type=gha,mode=max,scope=leadkit,ignore-error=true"


def test_the_builds_do_not_upload_a_build_record_artifact() -> None:
    """O build-push-action sobe por padrão um artefato por build, que ocupa o armazenamento do
    repositório privado."""
    assert load("deploy-image.yml")["env"]["DOCKER_BUILD_RECORD_UPLOAD"] == "false"


def test_the_swarm_rebuild_from_the_local_image_stays_on_the_default_builder() -> None:
    """O passo do `swarm` que reaproveita a tag para outra imagem (`LABEL ci.build=reaproveitada`)
    parte da imagem local `gerador-planos:ci`: só o builder padrão a enxerga, não o do cache."""
    plain = [s["run"] for s in steps_of("swarm") if "docker build" in str(s.get("run", ""))]
    assert len(plain) == 2  # Imagem histórica fixada e reconstrução local da candidata.
    (script,) = [script for script in plain if "FROM gerador-planos:ci" in script]
    assert "FROM gerador-planos:ci" in script
    assert "LABEL ci.build=reaproveitada" in script
    assert all("--builder" not in script for script in plain)
    assert [s for s in steps_of("image") if "docker build" in str(s.get("run", ""))] == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", ""])
def test_failed_or_absent_required_swarm_blocks_readiness(result: str) -> None:
    gate = load("deploy-image.yml")["jobs"]["validation"]
    assert run_gate(gate, {"changes": "success", "image": "success", "swarm": result}) != 0


def test_successful_or_inapplicable_swarm_concludes_the_packaging_gate() -> None:
    gate = load("deploy-image.yml")["jobs"]["validation"]
    assert run_gate(gate, {"changes": "success", "image": "success", "swarm": "success"}) == 0
    assert (
        run_gate(
            gate,
            {"changes": "success", "image": "success", "swarm": "skipped"},
            swarm_required=False,
        )
        == 0
    )
    assert (
        run_gate(
            gate,
            {"changes": "failure", "image": "success", "swarm": "skipped"},
            swarm_required=False,
        )
        != 0
    )


def test_inline_shell_and_python_commands_remain_syntactically_valid() -> None:
    for name in NAMES:
        for job in load(name)["jobs"].values():
            for step in job.get("steps", []):
                if "run" not in step:
                    continue
                script = re.sub(r"\$\{\{.*?\}\}", "checked-value", step["run"])
                result = subprocess.run(
                    ["bash", "-n"], input=script, text=True, capture_output=True, check=False
                )
                assert result.returncode == 0, (name, step["name"], result.stderr)
                for snippet in re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", script, re.DOTALL):
                    compile(snippet, step["name"], "exec")


def name_of_publisher(workflow: dict[str, Any]) -> str:
    writers = [
        name
        for name, job in workflow["jobs"].items()
        if "write" in job.get("permissions", {}).values()
    ]
    assert writers == ["publish"]
    assert workflow["jobs"]["publish"]["permissions"] == {"contents": "write", "packages": "write"}
    return writers[0]


def publisher_allowed(overrides: dict[str, Any]) -> bool:
    values = {
        "github.event_name": "push",
        "github.ref": "refs/heads/main",
        "github.repository": "LevyDeSales/leadkit",
        "github.event.repository.fork": False,
        "vars.LEADKIT_PUBLISH_ENABLED": "",
        "needs.app-tests.result": "success",
        "needs.packaging.result": "success",
        "needs.ci-ready.result": "success",
        **overrides,
    }
    condition = load("tests.yml")["jobs"]["publish"]["if"]
    for clause in condition.split("&&"):
        left, right = [part.strip() for part in clause.split("==")]
        expected = False if right == "false" else right.strip("'")
        if values.get(left) != expected:
            return False
    return True


def test_the_publisher_is_implemented_but_disabled_without_repository_opt_in() -> None:
    assert not publisher_allowed({})
    assert publisher_allowed({"vars.LEADKIT_PUBLISH_ENABLED": "true"})
    job = load("tests.yml")["jobs"]["publish"]
    assert job["needs"] == ["app-tests", "packaging", "ci-ready"]
    assert job["concurrency"]["cancel-in-progress"] is False
    names = [step["name"] for step in job["steps"]]
    assert (
        names.index("Build the image")
        < names.index("Create the release")
        < names.index("Push to GHCR")
    )
    script = next(step["run"] for step in job["steps"] if step["name"] == "Pick the version")
    assert "LEADKIT_RELEASE_BOOTSTRAP" in str(job) and "--allow-bootstrap" in script
    assert "git ls-remote" in script and "exit 1" not in script


@pytest.mark.parametrize(
    "overrides",
    [
        {"github.event_name": "pull_request"},
        {"github.ref": "refs/heads/feature"},
        {"github.repository": "example/leadkit"},
        {"github.event.repository.fork": True},
        *[
            {f"needs.{name}.result": result}
            for name in ("app-tests", "packaging", "ci-ready")
            for result in ("", "failure", "cancelled", "skipped")
        ],
    ],
)
def test_publisher_rejects_forks_prs_other_shas_and_missing_or_failed_checks(
    overrides: dict[str, Any],
) -> None:
    assert not publisher_allowed({"vars.LEADKIT_PUBLISH_ENABLED": "true", **overrides})


def test_swarm_rehearsal_accepts_the_real_guard_refusal(request, tmp_path: Path) -> None:
    """O ensaio precisa reconhecer a recusa real e ainda permitir rollback sem migrar.

    Só o transporte Docker é substituído: o deploy.sh e o Alembic/guard usam código real
    e PostgreSQL 16. Não há Swarm local; versões e disponibilidade ficam no ensaio remoto.
    """
    from test_swarm_scripts import Server, Staging

    engine = request.getfixturevalue("rollout_engine")

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    server = Server(tmp_path, Staging(staging_dir))
    docker = server.bin_dir / "docker"
    docker.rename(server.bin_dir / "docker-stub")
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == run && "${@: -1}" == migrate ]]; then\n'
        '  echo "$*" >> "$STUB_CALLS"\n'
        '  exec "$GUARD_PYTHON" -m alembic -c "$GUARD_ROOT/app/alembic.ini" upgrade head\n'
        "fi\n"
        'exec "$(dirname "$0")/docker-stub" "$@"\n'
    )
    docker.chmod(0o755)
    database_url = engine.url.render_as_string(hide_password=False)
    env = {
        "STUB_NETWORK": "1",
        "GUARD_PYTHON": sys.executable,
        "GUARD_ROOT": str(ROOT),
        "DATABASE_URL": database_url,
        "PYTHONPATH": str(ROOT),
        "IMAGE": "gerador-planos:ci",
    }
    with engine.begin() as connection:
        original = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        connection.exec_driver_sql("UPDATE alembic_version SET version_num = 'ci_futura'")
    try:
        refused = server.run("deploy.sh", **env)
        assert refused.returncode != 0
        assert "revisão incompatível" in refused.stderr
        assert not any(call.startswith("stack deploy") for call in server.calls)
        log = tmp_path / "back-sem-rollback.log"
        log.write_text(refused.stdout + refused.stderr)
        rehearsal = next(
            step["run"]
            for step in steps_of("swarm")
            if step.get("name", "").startswith("Update, break and go back")
        )
        # Executa as asserções do próprio workflow contra a saída do deploy real.
        assertions = rehearsal.split('cat "$RUNNER_TEMP/back-sem-rollback.log"')[-1]
        assertions = assertions.split('test "$(versions)"')[0]
        checked = subprocess.run(
            ["bash", "-e", "-c", assertions],
            capture_output=True,
            text=True,
            env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
            check=False,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert "revisão incompatível" in checked.stdout
        assert "a stack não foi mexida" in checked.stdout

        server.calls_file.write_text("")
        rollback = server.run("deploy.sh", ROLLBACK="1", **env)
        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        assert "sem migrations" in rollback.stdout
        assert any(call.startswith("stack deploy") for call in server.calls)
        assert not any(
            call.startswith("run ") and call.endswith(" migrate") for call in server.calls
        )
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "ci_futura"
            )
    finally:
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": original},
            )


@pytest.mark.parametrize(
    ("failure", "exit_code", "category"),
    [
        (
            "sqlalchemy.exc.OperationalError: password authentication failed",
            1,
            "database_authentication",
        ),
        ("psycopg.OperationalError: could not translate host name", 1, "database_dns"),
        ("psycopg.OperationalError: connection refused", 1, "database_unavailable"),
        ("ModuleNotFoundError: historical.module", 1, "module_missing"),
        ("RuntimeError: estrutura incompatível; objetos: private_table", 1, "schema_guard"),
        ("docker: daemon refused the request", 125, "docker_run"),
        ("unexpected historical failure", 1, "unclassified"),
        ("successful migration with private output", 0, None),
    ],
)
def test_historical_migration_failure_reports_only_safe_category_and_keeps_failure(
    tmp_path: Path, failure: str, exit_code: int, category: str | None
) -> None:
    """Executa o trecho real do workflow; somente o Docker externo é substituído."""
    import json

    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name") == "Rehearse real pre-projection bootstrap and forward recovery"
    )
    start = rehearsal.index('docker run --rm --network "${compat}_internal"')
    command = rehearsal[rehearsal.rfind("\n", 0, start) + 1 :]
    command = command[: command.index('test "$(psql_compat')]
    sentinel = "SYNTHETIC_PRIVATE_MIGRATION_CONTENT"
    script = (
        'docker() { printf "%s\\n" "$SYNTHETIC_FAILURE" >&2; return "$SYNTHETIC_EXIT"; }\n'
        + command
        + '\necho "migration failure was ignored"\n'
    )
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "compat": "synthetic-compat",
            "old_image": "synthetic:historical",
            "SYNTHETIC_FAILURE": f"{failure}\n{sentinel} postgresql://private@example.test/test",
            "SYNTHETIC_EXIT": str(exit_code),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_code
    if exit_code:
        assert json.loads(result.stdout) == {
            "stage": "historical_migration",
            "exit_code": exit_code,
            "category": category,
        }
    else:
        assert result.stdout == "migration failure was ignored\n"
    assert result.stderr == ""
    assert sentinel not in result.stdout + result.stderr
    assert "private" not in result.stdout + result.stderr


def test_historical_source_is_readable_by_image_user_before_build_without_opening_config(
    tmp_path: Path,
) -> None:
    """Usa tar real e o prefixo real do workflow sob umask077, sem Docker ou download."""
    import io
    import json
    import shlex
    import shutil
    import stat
    import tarfile

    archive = tmp_path / "public-source.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        for name in ("public-source", "public-source/app"):
            entry = tarfile.TarInfo(name)
            entry.type, entry.mode = tarfile.DIRTYPE, 0o755
            package.addfile(entry)
        entry = tarfile.TarInfo("public-source/app/alembic.ini")
        content = b"[alembic]\nscript_location = app/migrations\n"
        entry.size, entry.mode = len(content), 0o644
        package.addfile(entry, io.BytesIO(content))
    configuration = tmp_path / "private-config"
    configuration.mkdir(mode=0o700)
    env_file = configuration / "app.env"
    env_file.touch(mode=0o600)
    rehearsal = next(
        step["run"]
        for step in steps_of("swarm")
        if step.get("name") == "Rehearse real pre-projection bootstrap and forward recovery"
    )
    prefix = rehearsal[: rehearsal.index('cp "$CONF/stack.yaml"')]
    probe = (
        "import json,stat,sys; from pathlib import Path; p=Path(sys.argv[1]); "
        "print(json.dumps({name:stat.S_IMODE((p/name).stat().st_mode) "
        "for name in ('app','app/alembic.ini')}))"
    )
    script = (
        'curl() { cp "$SYNTHETIC_ARCHIVE" "$RUNNER_TEMP/compat-old.tar.gz"; }\n'
        'tar() { "$TEST_TAR" "$@"; }\n'
        f'docker() {{ "$TEST_PYTHON" -c {shlex.quote(probe)} "$source"; }}\n' + prefix
    )
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "SYNTHETIC_ARCHIVE": str(archive),
            "TEST_TAR": shutil.which("gtar") or shutil.which("tar"),
            "TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"app": 0o755, "app/alembic.ini": 0o644}
    assert stat.S_IMODE(configuration.stat().st_mode) == 0o700
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
