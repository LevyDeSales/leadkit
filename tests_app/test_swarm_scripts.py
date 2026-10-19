"""deploy.sh e backup.sh: o nome da stack vem da pasta de configuração, e o que está errado para antes de mexer.

O docker não está disponível na suíte: um `docker` de mentira no PATH registra cada chamada e
responde o que os scripts perguntam. O CI roda os mesmos scripts num Swarm de verdade (job `swarm`
do deploy-image.yml); aqui fica o que se confere sem Swarm: o nome que chega ao `docker stack
deploy`, e que as recusas acontecem antes de qualquer migration ou deploy.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SWARM_DIR = Path(__file__).resolve().parent.parent / "deploy" / "swarm"

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_CALLS"
case "$1 $2" in
  "image inspect")
    [[ "$*" == *--format* ]] && echo sha256:imagem
    exit 0 ;;
  "network inspect")
    if [[ "$*" == *traefik-public* ]]; then  # a rede do Traefik: o servidor tem uma sub-rede (ou mais)
      [[ -n "${STUB_NO_TRAEFIK_NETWORK:-}" ]] && exit 1
      echo "${STUB_TRAEFIK_SUBNETS-10.0.1.0/24}"
      exit 0
    fi
    [[ -n "${STUB_NETWORK:-}" ]] && exit 0
    exit 1 ;;
  "service inspect")
    if [[ "$*" == *Spec.Labels* ]]; then  # a regra do router que está no ar
      [[ -n "${STUB_LIVE_RULE:-}" ]] && { echo "$STUB_LIVE_RULE"; exit 0; }
      exit 1
    fi
    echo "servico_api"  # o estado das atualizações: sem nenhuma em curso
    exit 0 ;;
  "stack services") echo servico; exit 0 ;;
  "stack deploy")
    echo "STACK=${STACK:-} DOMAIN=${DOMAIN:-}" >> "$STUB_CALLS"
    # Os ajustes do stack.yaml que o deploy.sh exporta, para o teste conferir o que o Swarm recebe.
    env | grep -E '^(RATE_LIMIT_|[A-Z]+_MEMORY_|FORWARDED_ALLOW_IPS=)' | sort >> "$STUB_CALLS.settings"
    exit 0 ;;
  "service ps") exit 0 ;;
esac
case "$1" in
  ps)  # o contêiner do serviço do banco, só se o filtro pede o serviço que existe
    [[ "$*" == *"service.name=${STUB_DB_SERVICE:-}"* ]] && echo contêiner
    exit 0 ;;
  exec) printf 'dump' ; exit 0 ;;
esac
exit 0
"""

DOMAIN = "planos.example.test"
# O que os scripts leem do ambiente: o do desenvolvedor não pode vazar para os testes.
SCRIPT_VARIABLES = (
    "STACK",
    "DOMAIN",
    "IMAGE",
    "ROLLBACK",
    "BACKUP_DIR",
    "BACKUP_KEEP_DAYS",
    "RATE_LIMIT_AVERAGE",
    "RATE_LIMIT_BURST",
    "API_MEMORY_LIMIT",
    "API_MEMORY_RESERVATION",
    "WORKER_MEMORY_LIMIT",
    "WORKER_MEMORY_RESERVATION",
    "DB_MEMORY_LIMIT",
    "DB_MEMORY_RESERVATION",
    "FORWARDED_ALLOW_IPS",
)


class Staging:
    """Os scripts e o docker de mentira, copiados uma vez por processo; cada teste recebe um *hard
    link* deles, e não uma cópia.

    No macOS, a primeira execução de um arquivo que acabou de ser criado leva de 200 a 600 ms (a
    verificação de segurança do sistema), e as seguintes, uns 5 ms; um hard link do arquivo já
    verificado não paga nada. Com uma cópia por teste, cada um pagava duas vezes (o deploy.sh e o
    docker), uns 0,5 s: um sexto da suíte serial. No Linux não muda nada. Os testes só leem e executam
    os arquivos (nenhum os escreve), então dividir o inode é seguro."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.files: dict[Path, Path] = {}

    def copy(self, source: Path) -> Path:
        if source not in self.files:
            target = self.folder / f"{len(self.files)}-{source.name}"
            shutil.copy2(source, target)
            self.files[source] = target
        return self.files[source]

    def docker(self) -> Path:
        stub = SWARM_DIR / "docker"  # a chave do docker de mentira: o nome que nenhum arquivo tem
        if stub not in self.files:
            target = self.folder / "docker"
            target.write_text(FAKE_DOCKER)
            target.chmod(0o755)
            self.files[stub] = target
        return self.files[stub]

    @staticmethod
    def put(shared: Path, target: Path) -> None:
        try:
            os.link(shared, target)
        except OSError:  # outro sistema de arquivos, ou um que não liga arquivos
            shutil.copy2(shared, target)


class Server:
    """Uma pasta de configuração (stack.yaml, deploy.sh, app.env, db.env) e o docker de mentira."""

    def __init__(self, root: Path, staging: Staging) -> None:
        self.staging = staging
        self.folder = root / "swarm"
        self.folder.mkdir()
        for name in ("stack.yaml", "deploy.sh"):
            self.install(name)
        (self.folder / "db.env").write_text("POSTGRES_PASSWORD=exemplo\n")
        self.write_app_env()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        staging.put(staging.docker(), bin_dir / "docker")
        self.bin_dir = bin_dir
        self.calls_file = root / "calls.log"
        self.calls_file.touch()
        self.settings_file = Path(f"{self.calls_file}.settings")

    def install(self, name: str) -> None:
        """Põe na pasta de configuração o script (ou o stack.yaml) do repositório."""
        self.staging.put(self.staging.copy(SWARM_DIR / name), self.folder / name)

    def write_app_env(self, *lines: str) -> None:
        base = (f"PUBLIC_BASE_URL=https://{DOMAIN}", *lines)
        (self.folder / "app.env").write_text("\n".join(base) + "\n")

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Roda o script da pasta com o docker de mentira; `env` soma ao ambiente, que não leva STACK etc."""
        inherited = {
            name: value for name, value in os.environ.items() if name not in SCRIPT_VARIABLES
        }
        environment = {
            **inherited,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_CALLS": str(self.calls_file),
            **env,
        }
        return subprocess.run(
            [str(self.folder / script)],
            check=False,
            cwd=self.folder,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @property
    def calls(self) -> list[str]:
        return self.calls_file.read_text().splitlines()

    @property
    def deploy_environment(self) -> dict[str, str]:
        """As variáveis do stack.yaml que chegaram ao `docker stack deploy`, pelo ambiente dele."""
        if not self.settings_file.exists():
            return {}
        return dict(line.split("=", 1) for line in self.settings_file.read_text().splitlines())

    @property
    def settings(self) -> dict[str, str]:
        """Os ajustes do app.env (limite de envios, memória): tudo menos o FORWARDED_ALLOW_IPS."""
        return {k: v for k, v in self.deploy_environment.items() if k != "FORWARDED_ALLOW_IPS"}

    @property
    def forwarded_allow_ips(self) -> str | None:
        return self.deploy_environment.get("FORWARDED_ALLOW_IPS")

    def asked_for_the_traefik_network(self) -> bool:
        return any(
            call.startswith("network inspect") and "traefik-public" in call for call in self.calls
        )

    def touched_the_swarm(self) -> bool:
        """Se o script chegou a rodar migrations ou o deploy."""
        return any(call.startswith(("run ", "stack deploy")) for call in self.calls)


@pytest.fixture(scope="session")
def staging(tmp_path_factory: pytest.TempPathFactory) -> Staging:
    return Staging(tmp_path_factory.mktemp("swarm-scripts"))


@pytest.fixture
def server(tmp_path: Path, staging: Staging) -> Server:
    return Server(tmp_path, staging)


def stack_deploy_call(server: Server) -> str:
    return next(call for call in server.calls if call.startswith("stack deploy"))


def migrate_call(server: Server) -> str:
    return next(call for call in server.calls if call.startswith("run "))


def deploy(server: Server, **env: str) -> subprocess.CompletedProcess[str]:
    return server.run("deploy.sh", IMAGE="gerador-planos:abc123", **env)


def test_a_folder_without_stack_name_deploys_the_default_stack(server):
    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" gerador-planos")
    assert f"STACK=gerador-planos DOMAIN={DOMAIN}" in server.calls
    assert "--network gerador-planos_internal" in migrate_call(server)
    assert "--name gerador-planos-migrate" in migrate_call(server)


def test_the_stack_name_comes_from_the_app_env_of_the_folder(server):
    server.write_app_env("STACK_NAME=leadkit-test")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" leadkit-test")
    assert f"STACK=leadkit-test DOMAIN={DOMAIN}" in server.calls
    assert "--network leadkit-test_internal" in migrate_call(server)
    assert "--name leadkit-test-migrate" in migrate_call(server)
    assert "leadkit-test no ar com gerador-planos:abc123" in result.stdout


@pytest.mark.parametrize("line", ["STACK_NAME=", "STACK_NAME=\r"], ids=["empty", "carriage-return"])
def test_a_blank_stack_name_means_the_default_stack(server, line):
    server.write_app_env(line)

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" gerador-planos")


def test_the_stack_name_with_windows_line_endings_is_read_whole(server):
    server.write_app_env("STACK_NAME=leadkit-test\r")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" leadkit-test")


def test_the_same_stack_in_the_environment_is_accepted(server):
    server.write_app_env("STACK_NAME=leadkit-test")

    result = deploy(server, STACK="leadkit-test")

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" leadkit-test")


@pytest.mark.parametrize(
    ("in_folder", "in_environment"),
    [
        ("STACK_NAME=leadkit-test", "gerador-planos"),
        ("STACK_NAME=gerador-planos", "leadkit-test"),
        ("", "leadkit-test"),
    ],
    ids=[
        "dev-folder-with-default-stack",
        "default-folder-with-dev-stack",
        "no-name-with-dev-stack",
    ],
)
def test_a_stack_in_the_environment_that_differs_from_the_folder_stops_before_touching_anything(
    server, in_folder, in_environment
):
    server.write_app_env(in_folder)

    result = deploy(server, STACK=in_environment)

    assert result.returncode == 1
    assert f"STACK={in_environment} difere do STACK_NAME do app.env" in result.stderr
    assert "nada foi mexido" in result.stderr
    assert not server.touched_the_swarm()


@pytest.mark.parametrize("name", ["Planos Dev", "planos_dev", "-planos", "planos.dev", "a" * 41])
def test_an_invalid_stack_name_in_the_folder_stops_before_touching_anything(server, name):
    server.write_app_env(f"STACK_NAME={name}")

    result = deploy(server)

    assert result.returncode == 1
    assert "STACK_NAME" in result.stderr
    assert not server.touched_the_swarm()


def test_a_live_router_for_another_domain_stops_before_touching_anything(server):
    # A pasta errada: a configuração de outra stack, com o domínio dela, sobre uma stack no ar.
    result = deploy(server, STUB_NETWORK="1", STUB_LIVE_RULE="Host(`producao.example.test`)")

    assert result.returncode == 1
    assert "producao.example.test" in result.stderr
    assert DOMAIN in result.stderr
    assert "gerador-planos" in result.stderr
    assert "nada foi mexido" in result.stderr
    assert not server.touched_the_swarm()


def test_the_live_router_is_asked_of_the_stack_of_the_folder(server):
    server.write_app_env("STACK_NAME=leadkit-test")

    deploy(server, STUB_NETWORK="1", STUB_LIVE_RULE=f"Host(`{DOMAIN}`)")

    asked = [call for call in server.calls if "Spec.Labels" in call]
    assert len(asked) == 1
    assert "traefik.http.routers.leadkit-test.rule" in asked[0]
    assert asked[0].endswith(" leadkit-test_api")


def test_a_live_router_for_the_same_domain_lets_the_deploy_go_on(server):
    result = deploy(server, STUB_NETWORK="1", STUB_LIVE_RULE=f"Host(`{DOMAIN}`)")

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" gerador-planos")
    assert "--network gerador-planos_internal" in migrate_call(server)


def test_without_settings_in_the_app_env_the_stack_file_defaults_apply(server):
    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {}


def test_the_rate_limit_of_the_app_env_reaches_the_stack_deploy(server):
    server.write_app_env("RATE_LIMIT_AVERAGE=30", "RATE_LIMIT_BURST=60")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {"RATE_LIMIT_AVERAGE": "30", "RATE_LIMIT_BURST": "60"}


def test_a_setting_with_windows_line_endings_is_read_whole(server):
    server.write_app_env("RATE_LIMIT_AVERAGE=30\r")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {"RATE_LIMIT_AVERAGE": "30"}


def test_a_blank_setting_in_the_app_env_means_the_default(server):
    server.write_app_env("RATE_LIMIT_AVERAGE=", "RATE_LIMIT_BURST=")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {}


def test_a_setting_only_in_the_environment_is_accepted(server):
    result = deploy(server, RATE_LIMIT_BURST="40")

    assert result.returncode == 0, result.stderr
    assert server.settings == {"RATE_LIMIT_BURST": "40"}


def test_the_same_setting_in_the_environment_is_accepted(server):
    server.write_app_env("RATE_LIMIT_BURST=40")

    result = deploy(server, RATE_LIMIT_BURST="40")

    assert result.returncode == 0, result.stderr
    assert server.settings == {"RATE_LIMIT_BURST": "40"}


def test_a_setting_in_the_environment_that_differs_from_the_folder_stops_before_touching_anything(
    server,
):
    server.write_app_env("RATE_LIMIT_BURST=40")

    result = deploy(server, RATE_LIMIT_BURST="5")

    assert result.returncode == 1
    assert "RATE_LIMIT_BURST=5 difere do app.env (40)" in result.stderr
    assert "nada foi mexido" in result.stderr
    assert not server.touched_the_swarm()


@pytest.mark.parametrize("name", ["RATE_LIMIT_AVERAGE", "RATE_LIMIT_BURST"])
@pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5", "10 20", "1000000000", "10/min"])
def test_an_invalid_rate_limit_stops_before_touching_anything(server, name, value):
    # Um valor que o Traefik não entende descarta o middleware, e o router dos envios sai do ar.
    server.write_app_env(f"{name}={value}")

    result = deploy(server)

    assert result.returncode == 1
    assert name in result.stderr
    assert "não serve" in result.stderr
    assert not server.touched_the_swarm()


MEMORY_NAMES = [
    "API_MEMORY_LIMIT",
    "API_MEMORY_RESERVATION",
    "WORKER_MEMORY_LIMIT",
    "WORKER_MEMORY_RESERVATION",
    "DB_MEMORY_LIMIT",
    "DB_MEMORY_RESERVATION",
]


def test_the_memory_values_of_the_app_env_reach_the_stack_deploy(server):
    values = {
        "API_MEMORY_LIMIT": "768M",
        "WORKER_MEMORY_LIMIT": "2G",
        "DB_MEMORY_RESERVATION": "512m",
    }
    server.write_app_env(*(f"{name}={value}" for name, value in values.items()))

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == values


@pytest.mark.parametrize("value", ["512M", "1G", "1536m", "2g", "256MB", "1GB", "536870912", "64k"])
def test_a_memory_size_docker_understands_is_accepted(server, value):
    server.write_app_env(f"API_MEMORY_LIMIT={value}")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {"API_MEMORY_LIMIT": value}


@pytest.mark.parametrize("name", MEMORY_NAMES)
@pytest.mark.parametrize("value", ["abc", "0", "0M", "-1G", "1.5G", "512 M", "G", "512MiB", "1T"])
def test_an_invalid_memory_size_stops_before_touching_anything(server, name, value):
    # Um tamanho que o Docker não lê derruba o `docker stack deploy` no meio, depois das migrations.
    server.write_app_env(f"{name}={value}")

    result = deploy(server)

    assert result.returncode == 1
    assert name in result.stderr
    assert "não serve" in result.stderr
    assert not server.touched_the_swarm()


def test_a_memory_size_in_the_environment_that_differs_from_the_folder_stops_before_touching_anything(
    server,
):
    server.write_app_env("DB_MEMORY_LIMIT=2G")

    result = deploy(server, DB_MEMORY_LIMIT="1G")

    assert result.returncode == 1
    assert "DB_MEMORY_LIMIT=1G difere do app.env (2G)" in result.stderr
    assert not server.touched_the_swarm()


def test_the_api_trusts_the_subnet_of_the_traefik_network_by_default(server):
    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.forwarded_allow_ips == "10.0.1.0/24"
    assert "FORWARDED_ALLOW_IPS=10.0.1.0/24" in result.stdout


def test_every_subnet_of_the_traefik_network_is_trusted(server):
    result = deploy(server, STUB_TRAEFIK_SUBNETS="10.0.1.0/24,fd00:aaaa::/64")

    assert result.returncode == 0, result.stderr
    assert server.forwarded_allow_ips == "10.0.1.0/24,fd00:aaaa::/64"


def test_the_subnet_is_read_from_the_traefik_network_with_a_go_template(server):
    deploy(server)

    asked = next(call for call in server.calls if "traefik-public" in call)
    assert asked.startswith("network inspect traefik-public --format ")
    assert ".IPAM.Config" in asked
    assert ".Subnet" in asked


@pytest.mark.parametrize("value", ["*", "10.9.0.0/16", "10.9.0.0/16,172.20.0.5"])
def test_a_forwarded_allow_ips_in_the_app_env_wins_and_skips_the_network_lookup(server, value):
    server.write_app_env(f"FORWARDED_ALLOW_IPS={value}")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.forwarded_allow_ips == value
    assert not server.asked_for_the_traefik_network()


def test_a_blank_forwarded_allow_ips_in_the_app_env_means_the_traefik_network(server):
    server.write_app_env("FORWARDED_ALLOW_IPS=")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.forwarded_allow_ips == "10.0.1.0/24"


def test_without_the_traefik_network_the_deploy_stops_before_touching_anything(server):
    result = deploy(server, STUB_NO_TRAEFIK_NETWORK="1")

    assert result.returncode == 1
    assert "traefik-public" in result.stderr
    assert "FORWARDED_ALLOW_IPS" in result.stderr
    assert "nada foi mexido" in result.stderr
    assert not server.touched_the_swarm()


def test_a_network_without_a_subnet_stops_before_touching_anything(server):
    result = deploy(server, STUB_TRAEFIK_SUBNETS="")

    assert result.returncode == 1
    assert "sub-rede" in result.stderr
    assert not server.touched_the_swarm()


def test_a_forwarded_allow_ips_in_the_environment_that_differs_from_the_folder_stops(server):
    server.write_app_env("FORWARDED_ALLOW_IPS=10.9.0.0/16")

    result = deploy(server, FORWARDED_ALLOW_IPS="*")

    assert result.returncode == 1
    assert "FORWARDED_ALLOW_IPS=* difere do app.env (10.9.0.0/16)" in result.stderr
    assert not server.touched_the_swarm()


def test_a_forwarded_allow_ips_with_spaces_stops_before_touching_anything(server):
    server.write_app_env("FORWARDED_ALLOW_IPS=10.9.0.0/16 172.20.0.5")

    result = deploy(server)

    assert result.returncode == 1
    assert "FORWARDED_ALLOW_IPS" in result.stderr
    assert "não serve" in result.stderr
    assert not server.touched_the_swarm()


def backup(server: Server, **env: str) -> subprocess.CompletedProcess[str]:
    server.install("backup.sh")
    return server.run("backup.sh", BACKUP_DIR=str(server.folder / "dumps"), **env)


def test_the_backup_dumps_the_database_of_the_default_stack(server):
    result = backup(server, STUB_DB_SERVICE="gerador-planos_db")

    assert result.returncode == 0, result.stderr
    dumps = list((server.folder / "dumps").glob("mcc-*.dump"))
    assert [dump.read_text() for dump in dumps] == ["dump"]


def test_the_backup_dumps_the_database_of_the_stack_it_is_told(server):
    result = backup(server, STACK="leadkit-test", STUB_DB_SERVICE="leadkit-test_db")

    assert result.returncode == 0, result.stderr
    assert [dump.read_text() for dump in (server.folder / "dumps").glob("mcc-*.dump")] == ["dump"]


def test_the_backup_of_a_stack_without_database_fails_naming_the_service(server):
    # Só a stack padrão existe: o backup de outro nome não pode cair no banco dela.
    result = backup(server, STACK="leadkit-test", STUB_DB_SERVICE="gerador-planos_db")

    assert result.returncode == 1
    assert "leadkit-test_db" in result.stderr
    assert not list((server.folder / "dumps").glob("mcc-*.dump"))


def test_the_backup_refuses_an_invalid_stack_name(server):
    result = backup(server, STACK="../outro", STUB_DB_SERVICE="gerador-planos_db")

    assert result.returncode == 1
    assert "STACK" in result.stderr
