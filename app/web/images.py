"""A rota pública das imagens dos formulários: `/img/<sha256>.<extensão>`.

A página de resultado (a prévia do plano) e as prévias do admin pedem as imagens por aqui, e o
navegador as guarda para sempre: o endereço é o conteúdo (`AssetRef.url`), então uma imagem trocada
tem outro endereço e a antiga nunca muda (`Cache-Control: immutable`). O app lê o arquivo do storage
e o entrega; o bucket não é exposto.

Só sai o conteúdo que alguma imagem do admin ou alguma versão publicada tem
(`app.forms.assets.servable`): o sha256 de um arquivo qualquer do storage não abre nada. A resposta
não depende de login: o endereço de uma imagem só aparece numa página que a mostra, e a imagem é
conteúdo de formulário que vai a quem o abre.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app.adapters.assets import AssetStore, AssetUnavailable
from app.deps import get_deps
from app.errors import RetryableError, TerminalError
from app.forms import assets as form_assets
from app.forms.definition.images import ref_of_filename

logger = logging.getLogger(__name__)

IMMUTABLE = "public, max-age=31536000, immutable"
# O arquivo é só uma imagem: nada que ele traga pode rodar, e outra página não o embute sem
# querer.
IMAGE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "X-Robots-Tag": "noindex, nofollow",
}

router = APIRouter()


def _has(header: str, etag: str) -> bool:
    """O navegador já tem este ETag: o `If-None-Match` o lista, forte ou fraco (um proxy que comprime
    a resposta o torna fraco)."""
    offered = {item.strip().removeprefix("W/") for item in header.split(",")}
    return etag in offered or "*" in offered


def _refused(status_code: int) -> Response:
    return Response(status_code=status_code, headers={"Cache-Control": "no-store"})


@router.get("/img/{filename}")
def form_image(filename: str, request: Request) -> Response:
    ref = ref_of_filename(filename)
    if ref is None:
        return _refused(404)
    deps = get_deps()
    with deps.sessions() as session:
        if not form_assets.servable(session, ref):
            return _refused(404)
    etag = f'"{ref.sha256}"'
    headers = {"Cache-Control": IMMUTABLE, "ETag": etag, **IMAGE_HEADERS}
    if _has(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    try:
        content = AssetStore(deps.storage).read(ref)
    except AssetUnavailable:
        logger.error("imagem de formulário %s: o arquivo não está no storage", ref.filename)
        return _refused(404)
    except RetryableError:
        return Response(status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "5"})
    except TerminalError:
        logger.error("imagem de formulário %s: o storage recusou a leitura", ref.filename)
        return _refused(502)
    return Response(content, media_type=ref.content_type, headers=headers)
