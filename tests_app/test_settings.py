from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import settings as settings_module
from app.adapters.llm import FakePlanGenerator
from app.adapters.storage import LocalStorage
from app.deps import Deps, build_llm, build_storage
from app.destinations import SecretCipher
from app.intake import SubmissionLimits
from app.settings import (
    ENV_PERIOD_NAMES,
    PeriodNames,
    RetentionProblem,
    Settings,
    retention_problem,
)
from app.workflows import plan as plan_task


def settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LLM_MODE": "openai"}, "OPENAI_API_KEY"),
        ({"LLM_MODE": "openai", "OPENAI_API_KEY": "sk-teste"}, "OPENAI_MODEL"),
        ({"STORAGE_MODE": "s3"}, "S3_BUCKET"),
        ({"STORAGE_MODE": "s3", "S3_BUCKET": "b", "S3_ACCESS_KEY_ID": "k"}, "S3_SECRET_ACCESS_KEY"),
        ({"WEBHOOK_URL": "https://x.example/hook"}, "WEBHOOK_SECRET"),
        ({"TOKEN_SECRETS": "curto"}, "TOKEN_SECRETS"),
    ],
)
def test_real_modes_require_their_credentials(env: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        settings(**env).check()


def test_production_refuses_test_modes_and_plain_http() -> None:
    with pytest.raises(ValueError, match="LLM_MODE"):
        settings(APP_ENV="production").check()
    real = {
        "APP_ENV": "production",
        "LLM_MODE": "openai",
        "OPENAI_API_KEY": "sk-teste",
        "OPENAI_MODEL": "modelo",
        "PDF_MODE": "weasyprint",
        "STORAGE_MODE": "s3",
        "S3_BUCKET": "planos",
    }
    with pytest.raises(ValueError, match="https"):
        settings(**real).check()
    settings(**real, PUBLIC_BASE_URL="https://planos.example").check()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            {"LINK_TTL_DAYS": "30", "RETENTION_PDF_DAYS": "29"},
            "RETENTION_PDF_DAYS.*LINK_TTL_DAYS",
        ),
        (
            {"RETENTION_PDF_DAYS": "60", "RETENTION_DAYS": "59"},
            "RETENTION_DAYS.*RETENTION_PDF_DAYS",
        ),
    ],
)
def test_retention_periods_out_of_order_are_refused(env: dict[str, str], message: str) -> None:
    """O PDF dura pelo menos o link da lead, e o pedido inteiro pelo menos o PDF."""
    with pytest.raises(ValueError, match=message):
        settings(**env).check()


def test_retention_periods_may_end_with_the_lead_link() -> None:
    """O padrão depende disso: o PDF sai no dia em que o link da lead vence."""
    settings(LINK_TTL_DAYS="30", RETENTION_PDF_DAYS="30", RETENTION_DAYS="30").check()


FORM_NAMES = PeriodNames(
    link="a validade do link da lead",
    pdf="o prazo do PDF",
    request="o prazo do pedido",
    unit=" dias",
)


def test_one_function_judges_the_periods_for_the_environment_and_for_a_form() -> None:
    """A regra é uma só; cada lugar só diz como chama os prazos na mensagem."""
    periods = {"link_ttl_days": 30, "pdf_days": 29, "request_days": 60}

    env = retention_problem(**periods, names=ENV_PERIOD_NAMES)
    form = retention_problem(**periods, names=FORM_NAMES)

    assert env == RetentionProblem(
        "pdf",
        "RETENTION_PDF_DAYS (29) não pode ser menor que LINK_TTL_DAYS (30): o PDF fica enquanto "
        "o link da lead vale",
    )
    assert form == RetentionProblem(
        "pdf",
        "O prazo do PDF (29 dias) não pode ser menor que a validade do link da lead (30 dias): "
        "o PDF fica enquanto o link da lead vale",
    )
    request = retention_problem(link_ttl_days=30, pdf_days=60, request_days=59, names=FORM_NAMES)
    assert request == RetentionProblem(
        "request",
        "O prazo do pedido (59 dias) não pode ser menor que o prazo do PDF (60 dias): o pedido "
        "inteiro leva o PDF junto",
    )
    assert retention_problem(link_ttl_days=30, pdf_days=30, request_days=30) is None


def test_the_pdf_margin_over_the_link_is_one_number_for_every_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trocar `RETENTION_PDF_MARGIN_DAYS` muda a regra do ambiente e a do formulário juntas."""
    monkeypatch.setattr(settings_module, "RETENTION_PDF_MARGIN_DAYS", 7)
    periods = {"link_ttl_days": 30, "pdf_days": 36, "request_days": 60}

    problem = retention_problem(**periods, names=FORM_NAMES)

    assert problem is not None and problem.field == "pdf"
    assert "a validade do link da lead + 7 (37 dias)" in problem.message
    assert "mais 7 dias de folga" in problem.message
    with pytest.raises(ValueError, match=r"RETENTION_PDF_DAYS \(36\).*LINK_TTL_DAYS \+ 7 \(37\)"):
        settings(LINK_TTL_DAYS="30", RETENTION_PDF_DAYS="36", RETENTION_DAYS="60").check()
    settings(LINK_TTL_DAYS="30", RETENTION_PDF_DAYS="37", RETENTION_DAYS="60").check()
    assert retention_problem(**{**periods, "pdf_days": 37}, names=FORM_NAMES) is None


def test_fake_modes_build_fake_adapters() -> None:
    current = settings()
    assert isinstance(build_llm(current), FakePlanGenerator)
    assert isinstance(build_storage(current), LocalStorage)


def test_public_and_admin_routers_are_mounted(deps: Deps) -> None:
    from app.api.main import create_app

    client = TestClient(create_app(trigger=None))
    assert client.get("/admin/nao-existe").status_code == 404
    assert client.get("/static/nao-existe.css").status_code == 404
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_model_names_count_as_unset(blank: str) -> None:
    """Uma linha `OPENAI_FALLBACK_MODEL=` vazia no .env não pode virar um modelo de reserva."""
    given = settings(OPENAI_MODEL="gpt-5.4-mini", OPENAI_FALLBACK_MODEL=blank)

    assert given.openai_fallback_model is None
    assert settings(OPENAI_MODEL=blank).openai_model is None


@pytest.mark.parametrize("value", ["chave-de-mentira-curta", "{key},chave-de-mentira-curta"])
def test_an_encryption_key_that_is_not_fernet_stops_the_start_without_showing_it(
    value: str,
) -> None:
    value = value.format(key=Fernet.generate_key().decode())

    with pytest.raises(ValueError, match="WEBHOOK_ENCRYPTION_KEY") as raised:
        settings(WEBHOOK_ENCRYPTION_KEY=value).check()

    assert "chave-de-mentira" not in str(raised.value)


def test_encryption_keys_keep_their_order_and_a_blank_one_counts_as_unset() -> None:
    new, old = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    assert settings(WEBHOOK_ENCRYPTION_KEY=f" {new} , {old} ").encryption_keys == (new, old)
    assert settings(WEBHOOK_ENCRYPTION_KEY="  ").encryption_keys == ()
    assert SecretCipher.from_settings(settings(WEBHOOK_ENCRYPTION_KEY="")) is None


# --- limites de envio ---------------------------------------------------------------------------


def test_submission_limits_default_to_three_per_email_a_day_and_a_queue_of_200() -> None:
    current = settings()
    current.check()

    assert current.submission_limit_per_email == 3
    assert current.submission_limit_window_hours == 24
    assert current.submission_limit_queue == 200


@pytest.mark.parametrize(
    "name",
    ["SUBMISSION_LIMIT_PER_EMAIL", "SUBMISSION_LIMIT_WINDOW_HOURS", "SUBMISSION_LIMIT_QUEUE"],
)
def test_submission_limits_cannot_be_zero(name: str) -> None:
    """Sem desligar: um limite de 0 liberaria as rotas públicas sem aviso."""
    with pytest.raises(ValueError, match=name):
        settings(**{name: "0"})


def test_the_environment_sets_the_limits_the_routes_use(deps: Deps) -> None:
    deps.settings = settings(
        SUBMISSION_LIMIT_PER_EMAIL="2",
        SUBMISSION_LIMIT_WINDOW_HOURS="12",
        SUBMISSION_LIMIT_QUEUE="50",
    )

    assert deps.submission_limits == SubmissionLimits(
        per_email=2, window=timedelta(hours=12), queue_max=50
    )


# --- limite de uso da API de admin --------------------------------------------------------------


def test_the_admin_api_rate_limit_defaults_to_120_calls_a_minute_per_key() -> None:
    current = settings()
    current.check()

    assert current.api_rate_limit_per_minute == 120
    assert settings(API_RATE_LIMIT_PER_MINUTE="30").api_rate_limit_per_minute == 30


def test_the_admin_api_rate_limit_cannot_be_zero() -> None:
    """Sem desligar: um limite de 0 liberaria a API sem aviso, ou a travaria de vez."""
    with pytest.raises(ValueError, match="API_RATE_LIMIT_PER_MINUTE"):
        settings(API_RATE_LIMIT_PER_MINUTE="0")


def test_a_queue_ceiling_below_the_worker_slots_is_refused() -> None:
    with pytest.raises(ValueError, match="SUBMISSION_LIMIT_QUEUE.*WORKER_SLOTS"):
        settings(SUBMISSION_LIMIT_QUEUE="5", WORKER_SLOTS="10").check()
    settings(SUBMISSION_LIMIT_QUEUE="10", WORKER_SLOTS="10").check()


def test_a_window_longer_than_the_retention_is_refused() -> None:
    """A retenção apaga os envios que o limite conta: a janela não pode passar do prazo."""
    short_retention = {"LINK_TTL_DAYS": "1", "RETENTION_PDF_DAYS": "1", "RETENTION_DAYS": "2"}
    with pytest.raises(ValueError, match="SUBMISSION_LIMIT_WINDOW_HOURS.*RETENTION_DAYS"):
        settings(SUBMISSION_LIMIT_WINDOW_HOURS="49", **short_retention).check()
    settings(SUBMISSION_LIMIT_WINDOW_HOURS="48", **short_retention).check()


OPENAI = {"LLM_MODE": "openai", "OPENAI_API_KEY": "sk-teste", "OPENAI_MODEL": "modelo"}


def test_the_default_openai_timeout_fits_the_plan_task() -> None:
    """120 s x 2 modelos x 2 tentativas = 8 min, mais 1 min de folga: cabe nos 9 min da task."""
    settings(**OPENAI, OPENAI_FALLBACK_MODEL="reserva").check()
    settings(**OPENAI, OPENAI_FALLBACK_MODEL="reserva", OPENAI_TIMEOUT_SECONDS="120").check()


@pytest.mark.parametrize("fallback", [{"OPENAI_FALLBACK_MODEL": "reserva"}, {}])
def test_an_openai_timeout_that_outlasts_the_plan_task_is_refused(fallback: dict[str, str]) -> None:
    """Com 300 s o pior caso passa de 20 min: o Hatchet cancelaria a task no meio da chamada, e a
    resposta paga se perderia."""
    with pytest.raises(ValueError, match="OPENAI_TIMEOUT_SECONDS.*300"):
        settings(**OPENAI, **fallback, OPENAI_TIMEOUT_SECONDS="300").check()


def test_the_fallback_model_counts_in_the_worst_case() -> None:
    """Sem modelo de reserva são 2 chamadas, não 4: 200 s cabem sem ele, e com ele não."""
    settings(**OPENAI, OPENAI_TIMEOUT_SECONDS="200").check()
    with pytest.raises(ValueError, match="OPENAI_TIMEOUT_SECONDS"):
        settings(**OPENAI, OPENAI_FALLBACK_MODEL="reserva", OPENAI_TIMEOUT_SECONDS="200").check()


def test_the_openai_timeout_ceiling_follows_the_plan_task_timeout() -> None:
    """O teto sai do `execution_timeout` da task: 4 chamadas mais 1 min de folga cabem nele."""
    ceiling = (plan_task.execution_timeout.total_seconds() - 60) / 4
    with_fallback = {**OPENAI, "OPENAI_FALLBACK_MODEL": "reserva"}

    settings(**with_fallback, OPENAI_TIMEOUT_SECONDS=str(ceiling)).check()
    with pytest.raises(ValueError, match="OPENAI_TIMEOUT_SECONDS"):
        settings(**with_fallback, OPENAI_TIMEOUT_SECONDS=str(ceiling + 0.5)).check()


def test_the_openai_timeout_is_not_checked_with_the_fake_llm() -> None:
    settings(LLM_MODE="fake", OPENAI_TIMEOUT_SECONDS="900").check()
