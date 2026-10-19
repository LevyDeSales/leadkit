"""Dependências do processo (API ou worker), montadas uma vez a partir das configurações.

Cada adaptador real é carregado só quando o modo pede, por `<Classe>.from_settings(settings)`:
- LLM_MODE=openai        -> `app.adapters.openai_llm.OpenAIPlanGenerator`
- PDF_MODE=weasyprint    -> `app.adapters.weasyprint_pdf.WeasyPrintRenderer` (abre as imagens dos
                            formulários do mesmo storage)
- STORAGE_MODE=s3        -> `app.adapters.s3_storage.S3Storage`

O transporte do webhook está sempre montado: para onde cada evento vai (e se vai) é o destino do
webhook guardado no banco (`app.destinations`), aberto com a chave de WEBHOOK_ENCRYPTION_KEY.

As integrações com o Baserow (`baserow`) falam com a API dele pelo cliente de `app.integrations`; o
token de cada uma fica no banco, cifrado com a mesma chave.

O registro dos formulários (`forms`) atende os formulários guardados no banco
(`app.forms.stored.StoredRegistry`), com as mesmas sessões do resto do processo.

Os testes trocam o conjunto inteiro com `override_deps()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.assets import AssetStore
from app.adapters.llm import FakePlanGenerator, PlanGenerator
from app.adapters.pdf import FakePdfRenderer, PdfRenderer
from app.adapters.storage import ArtifactStorage, LocalStorage
from app.adapters.webhook import WebhookTransport
from app.db import make_engine, make_session_factory
from app.destinations import SecretCipher
from app.forms import FormRegistry
from app.forms.stored import StoredRegistry
from app.intake import SubmissionLimits
from app.integrations.baserow import BaserowTransport
from app.settings import Settings, get_settings
from app.tokens import TokenSigner


@dataclass
class Deps:
    settings: Settings
    sessions: sessionmaker[Session]
    signer: TokenSigner
    llm: PlanGenerator
    pdf: PdfRenderer
    storage: ArtifactStorage
    webhook: WebhookTransport
    cipher: SecretCipher | None  # None sem WEBHOOK_ENCRYPTION_KEY
    forms: FormRegistry
    # O cliente das integrações com o Baserow (`app.integrations`): um cliente HTTP só, trocado nos
    # testes por um sobre `httpx.MockTransport`.
    baserow: BaserowTransport = field(default_factory=BaserowTransport)

    @property
    def link_ttl(self) -> timedelta:
        return timedelta(days=self.settings.link_ttl_days)

    @property
    def submission_limits(self) -> SubmissionLimits:
        return SubmissionLimits(
            per_email=self.settings.submission_limit_per_email,
            window=timedelta(hours=self.settings.submission_limit_window_hours),
            queue_max=self.settings.submission_limit_queue,
        )


def build_llm(settings: Settings) -> PlanGenerator:
    if settings.llm_mode == "fake":
        return FakePlanGenerator()
    from app.adapters.openai_llm import OpenAIPlanGenerator

    return OpenAIPlanGenerator.from_settings(settings)


def build_pdf(settings: Settings, storage: ArtifactStorage | None = None) -> PdfRenderer:
    """O PDF real abre as imagens dos formulários (`asset:<nome>`) do `storage`, o mesmo do resto
    do processo."""
    if settings.pdf_mode == "fake":
        return FakePdfRenderer()
    from app.adapters.weasyprint_pdf import WeasyPrintRenderer

    assets = AssetStore(storage) if storage is not None else None
    return WeasyPrintRenderer.from_settings(settings, assets)


def build_storage(settings: Settings) -> ArtifactStorage:
    if settings.storage_mode == "local":
        return LocalStorage(settings.local_storage_dir)
    from app.adapters.s3_storage import S3Storage

    return S3Storage.from_settings(settings)


def build_deps(settings: Settings) -> Deps:
    sessions = make_session_factory(make_engine(settings.database_url))
    storage = build_storage(settings)
    return Deps(
        settings=settings,
        sessions=sessions,
        signer=TokenSigner(settings.signing_secrets),
        llm=build_llm(settings),
        pdf=build_pdf(settings, storage),
        storage=storage,
        webhook=WebhookTransport(),
        cipher=SecretCipher.from_settings(settings),
        forms=StoredRegistry(sessions),
        baserow=BaserowTransport(),
    )


_deps: Deps | None = None


def get_deps() -> Deps:
    global _deps
    if _deps is None:
        _deps = build_deps(get_settings())
    return _deps


def override_deps(deps: Deps | None) -> None:
    global _deps
    _deps = deps
