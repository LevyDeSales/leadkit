from __future__ import annotations

from functools import lru_cache
from typing import Literal, NamedTuple

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.plan_timeout import PLAN_EXECUTION_TIMEOUT, PLAN_MARGIN, llm_calls, worst_case

# Quantos dias o prazo do PDF precisa passar da validade do link da lead. Zero: o PDF pode sair no
# dia em que o link vence. É a margem de `retention_problem`, que o ambiente (`Settings.check`) e
# cada formulário (`app.forms.retention`) consultam; mudar o número muda os dois.
RETENTION_PDF_MARGIN_DAYS = 0


class PeriodNames(NamedTuple):
    """Como a mensagem de `retention_problem` chama cada prazo: pelo nome da variável de ambiente
    ou pelo texto da tela. `unit` vai depois de cada número."""

    link: str
    pdf: str
    request: str
    unit: str = ""


class RetentionProblem(NamedTuple):
    """Por que os prazos de retenção não servem: o prazo que está errado (`pdf` ou `request`) e a
    mensagem para quem o definiu."""

    field: Literal["pdf", "request"]
    message: str


ENV_PERIOD_NAMES = PeriodNames("LINK_TTL_DAYS", "RETENTION_PDF_DAYS", "RETENTION_DAYS")


def retention_problem(
    *,
    link_ttl_days: int,
    pdf_days: int,
    request_days: int,
    names: PeriodNames = ENV_PERIOD_NAMES,
) -> RetentionProblem | None:
    """A regra dos prazos de retenção, a mesma para os do ambiente e para os de um formulário: o
    prazo do PDF não pode ser menor que a validade do link da lead (mais `RETENTION_PDF_MARGIN_DAYS`)
    e o do pedido não pode ser menor que o do PDF. None quando servem."""
    unit = names.unit
    minimum = link_ttl_days + RETENTION_PDF_MARGIN_DAYS
    if pdf_days < minimum:
        floor = (
            f"{names.link} + {RETENTION_PDF_MARGIN_DAYS}"
            if RETENTION_PDF_MARGIN_DAYS
            else names.link
        )
        reason = "o PDF fica enquanto o link da lead vale"
        if RETENTION_PDF_MARGIN_DAYS:
            reason += f" e mais {RETENTION_PDF_MARGIN_DAYS} dias de folga"
        return RetentionProblem(
            "pdf",
            _capitalized(
                f"{names.pdf} ({pdf_days}{unit}) não pode ser menor que {floor} ({minimum}{unit}): "
                f"{reason}"
            ),
        )
    if request_days < pdf_days:
        return RetentionProblem(
            "request",
            _capitalized(
                f"{names.request} ({request_days}{unit}) não pode ser menor que {names.pdf} "
                f"({pdf_days}{unit}): o pedido inteiro leva o PDF junto"
            ),
        )
    return None


def _capitalized(message: str) -> str:
    return message[:1].upper() + message[1:]


class Settings(BaseSettings):
    """Configuração por variáveis de ambiente.

    Falha fechada: `APP_ENV` não tem default e os modos de teste (LLM, PDF e storage falsos) são
    recusados em produção. O token do Hatchet (`HATCHET_CLIENT_TOKEN`) é lido pelo próprio SDK.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = Field(alias="APP_ENV")
    database_url: str = Field(alias="DATABASE_URL")
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")
    token_secrets: str = Field(alias="TOKEN_SECRETS")
    link_ttl_days: int = Field(default=30, ge=1, le=365, alias="LINK_TTL_DAYS")

    llm_mode: Literal["openai", "fake"] = Field(default="openai", alias="LLM_MODE")
    pdf_mode: Literal["weasyprint", "fake"] = Field(default="weasyprint", alias="PDF_MODE")
    storage_mode: Literal["s3", "local"] = Field(default="s3", alias="STORAGE_MODE")
    local_storage_dir: str = Field(default="var/artifacts", alias="LOCAL_STORAGE_DIR")

    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str | None = Field(default=None, alias="OPENAI_MODEL")
    openai_fallback_model: str | None = Field(default=None, alias="OPENAI_FALLBACK_MODEL")
    openai_timeout_seconds: float = Field(default=120.0, gt=0, alias="OPENAI_TIMEOUT_SECONDS")
    openai_connect_timeout_seconds: float = Field(
        default=10.0, gt=0, alias="OPENAI_CONNECT_TIMEOUT_SECONDS"
    )

    s3_bucket: str | None = Field(default=None, alias="S3_BUCKET")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_endpoint_url: str | None = Field(default=None, alias="S3_ENDPOINT_URL")
    s3_access_key_id: str | None = Field(default=None, alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: SecretStr | None = Field(default=None, alias="S3_SECRET_ACCESS_KEY")

    # O destino do webhook mora no banco e é definido no admin (app.destinations). WEBHOOK_URL e
    # WEBHOOK_SECRET só valem na primeira subida depois da migration 0004: viram o destino padrão,
    # se ainda não houver um, e depois são ignoradas. WEBHOOK_ENCRYPTION_KEY cifra o segredo de
    # cada destino: uma ou mais chaves Fernet separadas por vírgula; a primeira cifra, todas abrem.
    webhook_url: str | None = Field(default=None, alias="WEBHOOK_URL")
    webhook_secret: str | None = Field(default=None, alias="WEBHOOK_SECRET")
    webhook_encryption_key: SecretStr | None = Field(default=None, alias="WEBHOOK_ENCRYPTION_KEY")

    hatchet_trigger_timeout_seconds: float = Field(
        default=3.0, gt=0, alias="HATCHET_TRIGGER_TIMEOUT_SECONDS"
    )
    worker_slots: int = Field(default=10, ge=1, le=200, alias="WORKER_SLOTS")

    # API de admin (app.admin.api): chamadas por minuto de cada chave de API, em balde por chave na
    # memória do processo (rajada de 30; a operação pesada custa 5 chamadas). Passou, 429 com
    # Retry-After. Com mais de uma réplica da API, o limite vale por réplica.
    api_rate_limit_per_minute: int = Field(
        default=120, ge=1, le=100_000, alias="API_RATE_LIMIT_PER_MINUTE"
    )

    # Painel admin: um usuário, senha em hash argon2 (como gerar: app/README.md). Sem os três, o
    # login fica recusado. O template do link para o Hatchet precisa conter `{run_id}`.
    admin_username: str | None = Field(default=None, alias="ADMIN_USERNAME")
    admin_password_hash: SecretStr | None = Field(default=None, alias="ADMIN_PASSWORD_HASH")
    admin_session_secret: SecretStr | None = Field(default=None, alias="ADMIN_SESSION_SECRET")
    hatchet_run_url_template: str | None = Field(default=None, alias="HATCHET_RUN_URL_TEMPLATE")

    # Retenção LGPD (app.retention): o pedido inteiro e a lead sem outros pedidos somem
    # RETENTION_DAYS depois da última mudança do job; o arquivo do PDF, RETENTION_PDF_DAYS depois
    # de gerado. Nada sai enquanto o link da lead vale, e check() recusa um prazo de PDF menor que
    # LINK_TTL_DAYS ou um de pedido menor que o do PDF. O agendamento é RETENTION_CRON (app.crons).
    retention_days: int = Field(default=60, ge=1, le=3650, alias="RETENTION_DAYS")
    retention_pdf_days: int = Field(default=30, ge=1, le=3650, alias="RETENTION_PDF_DAYS")

    # Limites dos envios públicos (app.intake), que são anônimos e cada um gasta a OpenAI e escreve
    # para o e-mail digitado. Por e-mail normalizado e por formulário: no máximo
    # SUBMISSION_LIMIT_PER_EMAIL envios em SUBMISSION_LIMIT_WINDOW_HOURS horas (429). Teto da fila:
    # com SUBMISSION_LIMIT_QUEUE pedidos em `queued` ou `running`, o envio novo espera (503). Os dois
    # respondem com Retry-After; reenviar a mesma chave de idempotência não conta. check() recusa
    # uma fila menor que WORKER_SLOTS e uma janela maior que RETENTION_DAYS.
    submission_limit_per_email: int = Field(
        default=3, ge=1, le=100, alias="SUBMISSION_LIMIT_PER_EMAIL"
    )
    submission_limit_window_hours: int = Field(
        default=24, ge=1, le=720, alias="SUBMISSION_LIMIT_WINDOW_HOURS"
    )
    submission_limit_queue: int = Field(default=200, ge=1, le=10000, alias="SUBMISSION_LIMIT_QUEUE")

    # Do build, não da operação: o Dockerfile grava os dois (ARG → ENV) e o rodapé do admin os mostra
    # (app/version.py). Sem eles, o rodapé diz `dev`.
    app_version: str | None = Field(default=None, alias="APP_VERSION")
    app_build_date: str | None = Field(default=None, alias="APP_BUILD_DATE")

    @field_validator(
        "openai_model",
        "openai_fallback_model",
        "webhook_url",
        "webhook_secret",
        "webhook_encryption_key",
        "app_version",
        "app_build_date",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        """Uma linha vazia no .env vale como ausente: `OPENAI_FALLBACK_MODEL=` não vira um modelo de
        reserva sem nome (tentado depois de uma falha do principal), nem
        `WEBHOOK_ENCRYPTION_KEY=` uma chave."""
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def signing_secrets(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.token_secrets.split(",") if s.strip())

    @property
    def encryption_keys(self) -> tuple[str, ...]:
        """As chaves de WEBHOOK_ENCRYPTION_KEY, na ordem: a primeira cifra, todas abrem."""
        raw = self.webhook_encryption_key.get_secret_value() if self.webhook_encryption_key else ""
        return tuple(key.strip() for key in raw.split(",") if key.strip())

    def check(self) -> None:
        """Valida combinações que o pydantic sozinho não cobre. Chamar na subida de API e worker."""
        if any(len(secret) < 32 for secret in self.signing_secrets) or not self.signing_secrets:
            raise ValueError("TOKEN_SECRETS precisa de segredos aleatórios com 32+ caracteres")
        if bool(self.webhook_url) != bool(self.webhook_secret):
            raise ValueError("WEBHOOK_URL e WEBHOOK_SECRET devem ser definidos juntos")
        self._check_encryption_keys()
        if self.llm_mode == "openai" and not (self.openai_api_key and self.openai_model):
            raise ValueError("LLM_MODE=openai exige OPENAI_API_KEY e OPENAI_MODEL")
        self._check_openai_timeout()
        if self.storage_mode == "s3" and not self.s3_bucket:
            raise ValueError("STORAGE_MODE=s3 exige S3_BUCKET")
        if bool(self.s3_access_key_id) != bool(self.s3_secret_access_key):
            raise ValueError("S3_ACCESS_KEY_ID e S3_SECRET_ACCESS_KEY devem ser definidos juntos")
        problem = retention_problem(
            link_ttl_days=self.link_ttl_days,
            pdf_days=self.retention_pdf_days,
            request_days=self.retention_days,
        )
        if problem:
            raise ValueError(problem.message)
        self._check_submission_limits()
        self._check_admin()
        if self.app_env != "production":
            return
        fakes = [
            name
            for name, value in (
                ("LLM_MODE", self.llm_mode == "fake"),
                ("PDF_MODE", self.pdf_mode == "fake"),
                ("STORAGE_MODE", self.storage_mode == "local"),
            )
            if value
        ]
        if fakes:
            raise ValueError(f"modos de teste não são permitidos em produção: {', '.join(fakes)}")
        if not self.public_base_url.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL precisa ser https em produção")

    def _check_openai_timeout(self) -> None:
        """No pior caso o adaptador gasta o timeout em todas as chamadas da tentativa. Passando do
        que a task `plan` tem para elas (o `execution_timeout` menos a folga), o Hatchet cancela a
        task no meio de uma chamada. Os números vêm de `app.plan_timeout`."""
        if self.llm_mode != "openai":
            return
        fallback = bool(self.openai_fallback_model)
        budget = PLAN_EXECUTION_TIMEOUT - PLAN_MARGIN
        if worst_case(self.openai_timeout_seconds, fallback=fallback) > budget:
            calls = llm_calls(fallback=fallback)
            raise ValueError(
                f"OPENAI_TIMEOUT_SECONDS ({self.openai_timeout_seconds:g}) é alto demais: no pior "
                f"caso são {calls} chamadas seguidas"
                f"{'' if fallback else ' (sem OPENAI_FALLBACK_MODEL)'}, e a task `plan` do Hatchet "
                f"só espera {budget.total_seconds():g} s por elas. O máximo é "
                f"{budget.total_seconds() / calls:g}"
            )

    def _check_submission_limits(self) -> None:
        if self.submission_limit_queue < self.worker_slots:
            raise ValueError(
                f"SUBMISSION_LIMIT_QUEUE ({self.submission_limit_queue}) não pode ser menor que "
                f"WORKER_SLOTS ({self.worker_slots}): os slots do worker nunca se encheriam"
            )
        if self.submission_limit_window_hours > self.retention_days * 24:
            raise ValueError(
                f"SUBMISSION_LIMIT_WINDOW_HOURS ({self.submission_limit_window_hours}) não pode "
                f"passar de RETENTION_DAYS ({self.retention_days}) em horas: o limite conta os "
                "envios, e a retenção os apaga"
            )

    def _check_encryption_keys(self) -> None:
        """Cada chave precisa ser uma chave Fernet. A mensagem nunca traz o valor recebido."""
        from cryptography.fernet import Fernet

        for key in self.encryption_keys:
            try:
                Fernet(key)
            except ValueError:
                raise ValueError(
                    "WEBHOOK_ENCRYPTION_KEY precisa de chaves Fernet (44 caracteres) separadas por "
                    "vírgula; como gerar: app/README.md"
                ) from None

    def _check_admin(self) -> None:
        admin = (self.admin_username, self.admin_password_hash, self.admin_session_secret)
        if any(admin) and not all(admin):
            raise ValueError(
                "ADMIN_USERNAME, ADMIN_PASSWORD_HASH e ADMIN_SESSION_SECRET devem ser definidos "
                "juntos"
            )
        secret = self.admin_session_secret.get_secret_value() if self.admin_session_secret else ""
        if secret and len(secret) < 32:
            raise ValueError("ADMIN_SESSION_SECRET precisa de 32+ caracteres aleatórios")
        password_hash = self.admin_password_hash
        if password_hash and not password_hash.get_secret_value().startswith("$argon2"):
            raise ValueError("ADMIN_PASSWORD_HASH precisa ser um hash argon2 (veja app/README.md)")
        template = self.hatchet_run_url_template
        if template and "{run_id}" not in template:
            raise ValueError("HATCHET_RUN_URL_TEMPLATE precisa conter {run_id}")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.check()
    return settings
