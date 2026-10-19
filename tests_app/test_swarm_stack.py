"""Rótulos do Traefik e limites de memória em deploy/swarm/stack.yaml.

O nome da stack decide os nomes do Traefik: duas stacks no mesmo Swarm com o mesmo nome de router,
serviço ou middleware fazem o Traefik descartar as duas. O docker não está disponível na suíte, então o teste lê o arquivo e aplica a
interpolação do compose (`${VAR:-padrão}`, `${VAR:?}`) como o `docker stack deploy` faz: só nos
valores, nunca nas chaves de um mapa, que é o motivo de os rótulos serem uma lista.
"""

import re
from pathlib import Path

import pytest
import yaml

STACK_FILE = Path(__file__).resolve().parent.parent / "deploy" / "swarm" / "stack.yaml"
APP_ENV_EXAMPLE = STACK_FILE.parent / "app.env.example"

# Os rótulos que a produção (stack gerador-planos) tem hoje, escritos à mão.
DEFAULT_LABELS = {
    "traefik.enable": "true",
    "traefik.docker.network": "traefik-public",
    "traefik.docker.lbswarm": "true",
    "traefik.http.routers.gerador-planos.rule": "Host(`planos.example.test`)",
    "traefik.http.routers.gerador-planos.entrypoints": "websecure",
    "traefik.http.routers.gerador-planos.tls": "true",
    "traefik.http.routers.gerador-planos.tls.certresolver": "letsencrypt",
    "traefik.http.routers.gerador-planos.middlewares": "gerador-planos-hsts,gerador-planos-compress",
    "traefik.http.middlewares.gerador-planos-hsts.headers.stsSeconds": "31536000",
    "traefik.http.middlewares.gerador-planos-compress.compress": "true",
    "traefik.http.services.gerador-planos.loadbalancer.server.port": "8000",
}

# O mesmo bloco para a stack leadkit-test.
LEADKIT_TEST_LABELS = {
    "traefik.enable": "true",
    "traefik.docker.network": "traefik-public",
    "traefik.docker.lbswarm": "true",
    "traefik.http.routers.leadkit-test.rule": "Host(`planos.example.test`)",
    "traefik.http.routers.leadkit-test.entrypoints": "websecure",
    "traefik.http.routers.leadkit-test.tls": "true",
    "traefik.http.routers.leadkit-test.tls.certresolver": "letsencrypt",
    "traefik.http.routers.leadkit-test.middlewares": "leadkit-test-hsts,leadkit-test-compress",
    "traefik.http.middlewares.leadkit-test-hsts.headers.stsSeconds": "31536000",
    "traefik.http.middlewares.leadkit-test-compress.compress": "true",
    "traefik.http.services.leadkit-test.loadbalancer.server.port": "8000",
}


def submit_labels(
    stack: str, domain: str = "planos.example.test", average: str = "10", burst: str = "20"
) -> dict[str, str]:
    """O router só dos envios (POST em /p/ e /v1/forms/), com o limite por IP, escrito à mão.

    O nome do router leva `_` porque o nome de uma stack não pode ter: outra stack nunca o repete.
    """
    router = f"traefik.http.routers.{stack}_submit"
    limit = f"traefik.http.middlewares.{stack}-ratelimit.ratelimit"
    return {
        f"{router}.rule": f"Host(`{domain}`) && Method(`POST`) "
        "&& (PathPrefix(`/p/`) || PathPrefix(`/v1/forms/`))",
        f"{router}.priority": "10000",
        f"{router}.entrypoints": "websecure",
        f"{router}.tls": "true",
        f"{router}.tls.certresolver": "letsencrypt",
        f"{router}.middlewares": f"{stack}-ratelimit,{stack}-hsts,{stack}-compress",
        f"{router}.service": stack,
        f"{limit}.average": average,
        f"{limit}.period": "1m",
        f"{limit}.burst": burst,
        f"{limit}.sourcecriterion.ipstrategy.depth": "0",
    }


_VARIABLE = re.compile(r"\$\$|\$\{(\w+)(?:(:-|:\?)([^}]*))?\}")
_TRAEFIK_NAME = re.compile(r"traefik\.http\.(routers|services|middlewares)\.([\w-]+)")


def _interpolate(value: str, env: dict[str, str]) -> str:
    """A interpolação do compose para `${VAR}`, `${VAR:-padrão}`, `${VAR:?}` e `$$`."""

    def substitute(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        name, operator, argument = match.groups()
        found = env.get(name, "")
        if found:
            return found
        if operator == ":-":
            return argument
        if operator == ":?":
            raise ValueError(f"{name} é obrigatória")
        return ""

    return _VARIABLE.sub(substitute, value)


def _api_labels_raw() -> list | dict:
    stack = yaml.safe_load(STACK_FILE.read_text())
    return stack["services"]["api"]["deploy"]["labels"]


def api_labels(env: dict[str, str]) -> dict[str, str]:
    """Os rótulos do serviço `api` como o Swarm os recebe, com as variáveis do ambiente aplicadas."""
    labels = {}
    for item in _api_labels_raw():
        key, _, value = _interpolate(item, env).partition("=")
        labels[key] = value
    return labels


def traefik_names(labels: dict[str, str]) -> set[str]:
    return {f"{kind}:{name}" for key in labels for kind, name in _TRAEFIK_NAME.findall(key)}


BASE_ENV = {"IMAGE": "gerador-planos:ci", "DOMAIN": "planos.example.test"}


def test_labels_are_a_list_so_the_stack_name_can_reach_the_keys():
    # No formato de mapa, o compose não interpola a chave, e o nome do router ficaria literal.
    assert isinstance(_api_labels_raw(), list)


def without_submit_router(labels: dict[str, str]) -> dict[str, str]:
    """Os rótulos de antes do router de envios: tudo o que não é dele nem do limite."""
    return {
        key: value
        for key, value in labels.items()
        if "_submit." not in key and "-ratelimit." not in key
    }


@pytest.mark.parametrize("stack", [None, ""], ids=["unset", "empty"])
def test_default_stack_keeps_todays_traefik_labels(stack):
    env = dict(BASE_ENV) if stack is None else {**BASE_ENV, "STACK": stack}

    labels = api_labels(env)

    # O que a produção tem hoje não muda: o router, os dois middlewares e o serviço seguem iguais.
    assert without_submit_router(labels) == DEFAULT_LABELS
    assert labels == {**DEFAULT_LABELS, **submit_labels("gerador-planos")}


def test_named_stack_takes_its_name_in_router_service_and_middlewares():
    labels = api_labels({**BASE_ENV, "STACK": "leadkit-test"})

    assert without_submit_router(labels) == LEADKIT_TEST_LABELS
    assert labels == {**LEADKIT_TEST_LABELS, **submit_labels("leadkit-test")}


def test_two_stacks_share_no_traefik_name():
    first = traefik_names(api_labels({**BASE_ENV, "STACK": "gerador-planos"}))
    second = traefik_names(api_labels({**BASE_ENV, "STACK": "gerador-planos-staging"}))

    assert first == {
        "routers:gerador-planos",
        "routers:gerador-planos_submit",
        "services:gerador-planos",
        "middlewares:gerador-planos-hsts",
        "middlewares:gerador-planos-compress",
        "middlewares:gerador-planos-ratelimit",
    }
    assert len(second) == 6
    assert first.isdisjoint(second)


def test_a_stack_named_like_another_stack_with_a_suffix_shares_no_name_either():
    # `gerador-planos-submit` é um nome de stack válido: o router de envios da primeira não pode ser
    # o router principal dela.
    first = traefik_names(api_labels({**BASE_ENV, "STACK": "gerador-planos"}))
    second = traefik_names(api_labels({**BASE_ENV, "STACK": "gerador-planos-submit"}))

    assert first.isdisjoint(second)


def test_submit_router_only_takes_posts_to_the_form_routes_of_the_domain():
    labels = api_labels(BASE_ENV)
    rule = labels["traefik.http.routers.gerador-planos_submit.rule"]

    assert rule == (
        "Host(`planos.example.test`) && Method(`POST`) "
        "&& (PathPrefix(`/p/`) || PathPrefix(`/v1/forms/`))"
    )


def test_submit_router_runs_the_same_hsts_and_compression_as_the_main_router():
    labels = api_labels(BASE_ENV)
    main = labels["traefik.http.routers.gerador-planos.middlewares"].split(",")
    submit = labels["traefik.http.routers.gerador-planos_submit.middlewares"].split(",")

    # O limite vem primeiro: o pedido barrado não gasta o resto.
    assert submit == ["gerador-planos-ratelimit", *main]


def test_submit_router_wins_over_the_main_router_for_any_domain():
    # Sem prioridade explícita, o Traefik usa o tamanho da regra. A do router principal cresce com o
    # domínio (no máximo 253 caracteres), então a prioridade do de envios fica acima do maior caso.
    longest_domain = "a" * 253
    labels = api_labels({**BASE_ENV, "DOMAIN": longest_domain})
    main_priority = len(labels["traefik.http.routers.gerador-planos.rule"])

    assert int(labels["traefik.http.routers.gerador-planos_submit.priority"]) > main_priority


def test_submit_router_limits_by_the_address_of_the_connection():
    # O Traefik do servidor tem as portas em modo host, então o endereço da conexão é o do visitante.
    # Pelo X-Forwarded-For, que o cliente escreve, o limite se contornaria.
    labels = api_labels(BASE_ENV)
    prefix = "traefik.http.middlewares.gerador-planos-ratelimit.ratelimit.sourcecriterion"

    assert labels[f"{prefix}.ipstrategy.depth"] == "0"
    assert not [key for key in labels if "requestheadername" in key.lower()]


def test_the_rate_limit_is_conservative_by_default():
    labels = api_labels(BASE_ENV)
    prefix = "traefik.http.middlewares.gerador-planos-ratelimit.ratelimit"

    # 10 pedidos por minuto por IP, com rajada de 20: um escritório inteiro atrás de um IP só passa.
    assert (labels[f"{prefix}.average"], labels[f"{prefix}.period"], labels[f"{prefix}.burst"]) == (
        "10",
        "1m",
        "20",
    )


def test_the_rate_limit_takes_average_and_burst_from_the_environment():
    env = {
        **BASE_ENV,
        "STACK": "leadkit-test",
        "RATE_LIMIT_AVERAGE": "30",
        "RATE_LIMIT_BURST": "60",
    }

    labels = api_labels(env)

    assert labels == {
        **LEADKIT_TEST_LABELS,
        **submit_labels("leadkit-test", average="30", burst="60"),
    }


@pytest.mark.parametrize("blank", ["", None], ids=["empty", "unset"])
def test_a_blank_rate_limit_variable_keeps_the_default(blank):
    env = {**BASE_ENV, "RATE_LIMIT_AVERAGE": blank} if blank is not None else dict(BASE_ENV)

    labels = api_labels(env)

    assert labels["traefik.http.middlewares.gerador-planos-ratelimit.ratelimit.average"] == "10"


def test_the_domain_is_still_required():
    with pytest.raises(ValueError, match="DOMAIN"):
        api_labels({"IMAGE": "gerador-planos:ci", "STACK": "leadkit-test"})


def documented_defaults() -> dict[str, str]:
    """Os ajustes opcionais do app.env.example: linhas `# NOME=valor`, o padrão que vale sem a linha."""
    found = {}
    for line in APP_ENV_EXAMPLE.read_text().splitlines():
        match = re.fullmatch(r"#\s*([A-Z][A-Z0-9_]*)=(\S*)", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def test_the_rate_limit_documented_in_the_app_env_example_is_the_stack_file_default():
    labels = api_labels(BASE_ENV)
    prefix = "traefik.http.middlewares.gerador-planos-ratelimit.ratelimit"
    documented = documented_defaults()

    assert documented["RATE_LIMIT_AVERAGE"] == labels[f"{prefix}.average"]
    assert documented["RATE_LIMIT_BURST"] == labels[f"{prefix}.burst"]


# Os limites e as reservas de memória de cada serviço: a variável do app.env e o padrão do stack.yaml.
MEMORY_SETTINGS = {
    "API_MEMORY_LIMIT": ("api", "limits", "512M"),
    "API_MEMORY_RESERVATION": ("api", "reservations", "128M"),
    "WORKER_MEMORY_LIMIT": ("worker", "limits", "1536M"),
    "WORKER_MEMORY_RESERVATION": ("worker", "reservations", "512M"),
    "DB_MEMORY_LIMIT": ("db", "limits", "1G"),
    "DB_MEMORY_RESERVATION": ("db", "reservations", "256M"),
}


def memory(service: str, kind: str, env: dict[str, str]) -> str:
    """O valor de `deploy.resources.<kind>.memory` do serviço, com as variáveis do ambiente aplicadas."""
    stack = yaml.safe_load(STACK_FILE.read_text())
    resources = stack["services"][service]["deploy"]["resources"]
    return _interpolate(resources[kind]["memory"], env)


def megabytes(size: str) -> int:
    units = {"M": 1, "G": 1024}
    return int(size[:-1]) * units[size[-1]]


@pytest.mark.parametrize(
    ("name", "expected"), list(MEMORY_SETTINGS.items()), ids=list(MEMORY_SETTINGS)
)
def test_every_service_has_a_generous_memory_limit_and_a_reservation_by_default(name, expected):
    service, kind, default = expected

    assert memory(service, kind, BASE_ENV) == default


@pytest.mark.parametrize(
    ("name", "expected"), list(MEMORY_SETTINGS.items()), ids=list(MEMORY_SETTINGS)
)
def test_each_memory_value_takes_its_variable(name, expected):
    service, kind, _ = expected

    assert memory(service, kind, {**BASE_ENV, name: "2G"}) == "2G"


@pytest.mark.parametrize(
    ("name", "expected"), list(MEMORY_SETTINGS.items()), ids=list(MEMORY_SETTINGS)
)
def test_a_blank_memory_variable_keeps_the_default(name, expected):
    service, kind, default = expected

    assert memory(service, kind, {**BASE_ENV, name: ""}) == default


@pytest.mark.parametrize("service", ["api", "worker", "db"])
def test_the_reservation_stays_below_the_limit_by_default(service):
    assert megabytes(memory(service, "reservations", BASE_ENV)) < megabytes(
        memory(service, "limits", BASE_ENV)
    )


def test_the_memory_defaults_documented_in_the_app_env_example_are_the_stack_file_defaults():
    documented = documented_defaults()

    for name, (_, _, default) in MEMORY_SETTINGS.items():
        assert documented[name] == default, name


def api_environment(env: dict[str, str]) -> dict[str, str]:
    """O `environment` do serviço `api` como o Swarm o recebe, com as variáveis aplicadas."""
    stack = yaml.safe_load(STACK_FILE.read_text())
    return {
        name: _interpolate(str(value), env)
        for name, value in stack["services"]["api"]["environment"].items()
    }


def test_the_api_trusts_the_forwarded_headers_of_the_networks_the_deploy_passes():
    environment = api_environment({**BASE_ENV, "FORWARDED_ALLOW_IPS": "10.0.1.0/24,fd00:1::/64"})

    assert environment == {"FORWARDED_ALLOW_IPS": "10.0.1.0/24,fd00:1::/64"}


@pytest.mark.parametrize("blank", ["", None], ids=["empty", "unset"])
def test_a_deploy_by_hand_without_the_variable_keeps_trusting_every_sender(blank):
    # O que a stack fazia antes: o deploy.sh passa a rede do Traefik, e quem roda o comando à mão sem
    # a variável não ganha uma API que recusa o IP de quem acessa por engano de configuração.
    env = {**BASE_ENV, "FORWARDED_ALLOW_IPS": blank} if blank is not None else dict(BASE_ENV)

    assert api_environment(env) == {"FORWARDED_ALLOW_IPS": "*"}


def test_only_the_api_receives_the_forwarded_variable():
    stack = yaml.safe_load(STACK_FILE.read_text())

    for service in ("worker", "db"):
        assert "FORWARDED_ALLOW_IPS" not in stack["services"][service].get("environment", {})
