"""PDF real: template Jinja (HTML) do formulário renderizado pelo WeasyPrint.

O template vem do texto da definição do formulário, renderizado no ambiente isolado
(`FormSpec.templates`). O PDF só carrega `data:`, arquivos de `app/assets/` e as imagens do
formulário (`asset:<nome>`), que o buscador abre do storage pelo conteúdo que a versão fixou
(`FormSpec.templates.assets`, `app.adapters.assets`).
"""

from __future__ import annotations

import mimetypes
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from pydantic import ValidationError
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration
from weasyprint.urls import FatalURLFetchingError, URLFetcher, URLFetcherResponse

from app.adapters.assets import AssetStore, AssetUnavailable
from app.adapters.pdf import RenderedPdf
from app.branding import BrandProblem, installation_brand
from app.errors import FailureCode, RetryableError, TerminalError
from app.forms import FormSpec
from app.forms.dates import long_date
from app.forms.definition.images import SCHEME, AssetRef
from app.forms.render_guard import RenderLimit, run_within_limits
from app.forms.sandbox import TemplateProblem
from app.settings import Settings

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
FONTS_CSS = ASSETS_DIR / "fonts" / "fonts.css"
# O WeasyPrint (Pango/GLib) não é thread-safe e o worker roda tasks síncronas num pool de threads:
# a PoC mediu corrupção de memória com 8 threads e nenhuma com o lock. Uma renderização por vez no
# processo; para renderizar mais em paralelo, suba mais processos de worker.
_RENDER_LOCK = threading.Lock()
# O prazo e o crescimento de memória do processo que o motor pode levar com o lock na mão (ver
# `app.forms.render_guard`). Um plano normal leva de décimos de segundo a poucos segundos; sem prazo,
# um HTML pequeno que vira cem mil parágrafos levava 43 s, e todo PDF do processo esperava. O prazo
# fica abaixo do `execution_timeout` da task `render` (2 min), que ainda lê e grava no storage.
PDF_SECONDS = 45.0
PDF_MEMORY = 384 * 2**20


class AssetsOnlyFetcher(URLFetcher):
    """Abre só `data:`, arquivos dentro de `app/assets/` e as imagens do formulário (`asset:<nome>`,
    pelo conteúdo que `assets` fixa para cada nome). Qualquer outra URL (http, https, outro arquivo
    do servidor) interrompe a renderização em vez de ser buscada. Uma imagem que o formulário não
    tem, ou que o storage não entrega, também."""

    def __init__(
        self, assets: Mapping[str, AssetRef] | None = None, store: AssetStore | None = None
    ) -> None:
        super().__init__(allowed_protocols={"data"}, allow_redirects=False, fail_on_errors=True)
        self._brand = installation_brand()
        self._assets = assets or {}
        self._store = store

    def fetch(self, url: str, headers: Any = None) -> URLFetcherResponse:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme == "brand" and not parts.netloc:
            try:
                content = self._brand.read(parts.path)
            except BrandProblem:
                raise AssetUnavailable("recurso da marca indisponível") from None
            return URLFetcherResponse(
                url,
                content,
                {"Content-Type": mimetypes.guess_type(parts.path)[0] or "application/octet-stream"},
            )
        if scheme == SCHEME.rstrip(":"):
            ref = self._assets.get(unquote(parts.path)) if not parts.netloc else None
            if ref is None or self._store is None:
                raise AssetUnavailable("o formulário não tem a imagem")
            return URLFetcherResponse(
                url, self._store.read(ref), {"Content-Type": ref.content_type}
            )
        if scheme == "file" and not parts.netloc:
            path = Path(url2pathname(parts.path)).resolve()
            # Caminhos de templates imutáveis são aliases lógicos; não contêm o kit privado.
            relative = path.relative_to(ASSETS_DIR) if path.is_relative_to(ASSETS_DIR) else None
            if relative and relative.parts[:2] in {
                ("brands", "leadkit"),
                ("brands", "academia-de-contadores"),
            }:
                resource = Path(*relative.parts[2:]).as_posix()
                if resource.startswith("assets/brand/svg/logo-"):
                    resource = relative.name
                try:
                    content = self._brand.read(resource)
                except BrandProblem:
                    raise AssetUnavailable("recurso da marca indisponível") from None
                return URLFetcherResponse(
                    url,
                    content,
                    {
                        "Content-Type": mimetypes.guess_type(resource)[0]
                        or "application/octet-stream"
                    },
                )
            if relative and path.is_file():
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                return URLFetcherResponse(url, path.read_bytes(), {"Content-Type": content_type})
        return super().fetch(url, headers)  # aceita data:; recusa o resto (protocolo não permitido)


def _fetch_failure(cause: BaseException | None) -> Exception:
    """O que uma falha do buscador diz ao pedido. Falha passageira do storage ao ler uma imagem
    (S3 fora do ar por um instante) repete; o resto é definitivo, com mensagem fixa: o nome da
    imagem pode vir de um template que monta o endereço, então ele não entra."""
    if isinstance(cause, RetryableError):
        return RetryableError(cause.code, cause.detail)
    if isinstance(cause, AssetUnavailable | TerminalError):
        return TerminalError(
            FailureCode.RENDER_FAILED, "template do PDF pediu uma imagem do formulário indisponível"
        )
    return TerminalError(
        FailureCode.RENDER_FAILED, "template do PDF pediu um recurso fora de app/assets"
    )


class WeasyPrintRenderer:
    """Variáveis do template: `plan` (plano validado pelo modelo do formulário, sem os campos
    internos), `lead.name`, `form.title`, `form.slug`, `scores` (as notas do envio que a definição
    liberou para o PDF) e `generated_date` (data por extenso em America/Sao_Paulo)."""

    def __init__(self, assets: AssetStore | None = None) -> None:
        """`assets` é de onde vêm as imagens do formulário (`asset:<nome>`); sem ele, o PDF que
        pede uma falha."""
        self._assets = assets
        self._fonts: FontConfiguration | None = None  # criado na 1ª renderização, sob o lock

    @classmethod
    def from_settings(
        cls, settings: Settings, assets: AssetStore | None = None
    ) -> WeasyPrintRenderer:
        return cls(assets)

    def render(
        self, spec: FormSpec, document: dict[str, Any], context: dict[str, Any]
    ) -> RenderedPdf:
        """Erros de template, de dados ou do motor viram `TerminalError(RENDER_FAILED)` com
        mensagem fixa e sem a exceção original encadeada (ela pode trazer dados do lead)."""
        html = self._html(spec, document, context)
        fetcher = AssetsOnlyFetcher(spec.templates.assets, self._assets)
        try:
            with _RENDER_LOCK:
                if self._fonts is None:
                    self._fonts = FontConfiguration()
                try:
                    rendered, content = run_within_limits(
                        lambda: self._engine(html, spec, fetcher),
                        seconds=PDF_SECONDS,
                        memory=PDF_MEMORY,
                    )
                except RenderLimit:
                    # O corte pode ter caído no meio do registro das fontes da marca (o `@font-face`
                    # do `fonts.css`): a configuração fica pela metade e os PDFs seguintes sairiam
                    # sem elas. Ainda com o lock, a próxima renderização refaz uma nova.
                    self._fonts = None
                    raise
        except FatalURLFetchingError as exc:
            raise _fetch_failure(exc.__cause__) from None
        except RenderLimit as exc:  # a mensagem é fixa, sem dado da lead
            raise TerminalError(FailureCode.RENDER_FAILED, f"o PDF {exc}") from None
        except Exception as exc:  # noqa: BLE001 - qualquer falha do motor é definitiva; sem a causa
            raise TerminalError(
                FailureCode.RENDER_FAILED, f"WeasyPrint falhou ({type(exc).__name__})"
            ) from None
        return RenderedPdf(content=content, renderer="weasyprint", page_count=len(rendered.pages))

    def _engine(self, html: str, spec: FormSpec, fetcher: AssetsOnlyFetcher) -> tuple[Any, bytes]:
        """O motor: o CSS, o layout e o PDF. Roda com o lock e dentro do prazo e do teto de memória."""
        rendered = HTML(string=html, base_url=str(ASSETS_DIR), url_fetcher=fetcher).render(
            stylesheets=self._stylesheets(spec, fetcher), font_config=self._fonts
        )
        return rendered, rendered.write_pdf()

    def _html(self, spec: FormSpec, document: dict[str, Any], context: dict[str, Any]) -> str:
        try:
            plan = spec.plan_model.model_validate(document)
        except ValidationError:
            raise TerminalError(
                FailureCode.RENDER_FAILED, "plano não valida com o modelo do formulário"
            ) from None
        variables = {
            "plan": plan,
            "lead": context["lead"],
            "form": context["form"],
            "scores": context.get("scores") or {},
            "generated_date": long_date(context["generated_at"]),
        }
        try:
            return spec.templates.render_pdf(**variables)
        except TemplateProblem as exc:  # só o template, a linha e o tipo do erro, sem valores
            raise TerminalError(
                FailureCode.RENDER_FAILED, f"template do PDF falhou ({exc})"
            ) from None
        except Exception as exc:  # noqa: BLE001 - erro do template é definitivo; sem a causa
            raise TerminalError(
                FailureCode.RENDER_FAILED, f"template do PDF falhou ({type(exc).__name__})"
            ) from None

    def _stylesheets(self, spec: FormSpec, fetcher: AssetsOnlyFetcher) -> list[CSS]:
        """Fontes do app e o CSS da definição. As URLs relativas do CSS apontam para `app/assets/`.
        Usa a configuração de fontes do lock."""
        sheets = [CSS(filename=FONTS_CSS, url_fetcher=fetcher, font_config=self._fonts)]
        options: dict[str, Any] = {
            "base_url": str(ASSETS_DIR),
            "url_fetcher": fetcher,
            "font_config": self._fonts,
        }
        if spec.templates.pdf_css.strip():
            sheets.append(CSS(string=spec.templates.pdf_css, **options))
        return sheets
