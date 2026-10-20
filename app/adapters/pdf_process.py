"""Supervisiona um motor PDF descartável e drena o IPC antes de esperar sua saída."""

from __future__ import annotations

import base64
import os
import selectors
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from weasyprint.urls import URLFetcherResponse

from app.adapters.pdf_ipc import encode, take
from app.forms.render_guard import RenderLimit

STARTUP_SECONDS = 15.0
MEMORY_EXIT = 74
UNRAISABLE_EXIT = 75


class RenderProcessError(Exception):
    """O motor isolado não devolveu um PDF válido, sem detalhes da entrada."""


class RenderCleanupError(RenderProcessError):
    """Uma exceção não propagável encerrou o motor; preserva o sinal sem revelar o objeto."""


def _process_failure(code: int, memory: int) -> None:
    if code == MEMORY_EXIT:
        raise RenderLimit(f"passou de {memory // 2**20} MB de memória")
    if code == UNRAISABLE_EXIT:
        raise RenderCleanupError("falha na limpeza do motor PDF")
    raise RenderProcessError("motor PDF interrompido")


def _stop(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def render_pdf(
    html: str,
    css: str,
    fonts_css: str,
    base_url: str,
    fetch: Callable[[str], URLFetcherResponse],
    *,
    seconds: float,
    memory: int,
) -> tuple[bytes, int]:
    """Só HTML/CSS e recursos pedidos atravessam o socket; clientes permanecem no pai.

    O prazo do motor começa após importar suas bibliotecas. Startup tem prazo próprio. A leitura
    síncrona de um recurso conserva os timeouts do storage; o prazo é reavaliado assim que retorna.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "DYLD_FALLBACK_LIBRARY_PATH",
            "FONTCONFIG_FILE",
            "FONTCONFIG_PATH",
            "XDG_CACHE_HOME",
        }
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    channel, child = socket.socketpair()
    with channel, child:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.adapters.pdf_worker", str(child.fileno())],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            pass_fds=(child.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child.close()
        try:
            channel.setblocking(False)
            incoming = bytearray()
            outgoing = bytearray()
            deadline = time.monotonic() + STARTUP_SECONDS
            started = False
            with selectors.DefaultSelector() as selector:
                selector.register(channel, selectors.EVENT_READ)
                while True:
                    if time.monotonic() >= deadline:
                        if not started:
                            raise RenderProcessError("motor PDF indisponível")
                        raise RenderLimit(f"passou do prazo de {seconds:g} s")
                    selector.modify(
                        channel,
                        selectors.EVENT_READ | (selectors.EVENT_WRITE if outgoing else 0),
                    )
                    for _, events in selector.select(min(0.05, deadline - time.monotonic())):
                        if events & selectors.EVENT_WRITE:
                            try:
                                sent = channel.send(outgoing[:65536])
                            except BlockingIOError:
                                pass
                            else:
                                del outgoing[:sent]
                        if events & selectors.EVENT_READ:
                            try:
                                chunk = channel.recv(65536)
                            except BlockingIOError:
                                continue
                            if not chunk:
                                _process_failure(process.wait(timeout=0.5), memory)
                            incoming.extend(chunk)
                            while (message := take(incoming)) is not None:
                                kind = message.get("type")
                                if kind == "ready" and not started:
                                    started = True
                                    deadline = time.monotonic() + seconds
                                    outgoing.extend(
                                        encode(
                                            {
                                                "html": html,
                                                "css": css,
                                                "fonts_css": fonts_css,
                                                "base_url": base_url,
                                                "memory": memory,
                                            }
                                        )
                                    )
                                elif kind == "fetch" and started:
                                    response = fetch(message["url"])
                                    try:
                                        body = response.read()
                                    finally:
                                        response.close()
                                    if time.monotonic() >= deadline:
                                        raise RenderLimit(f"passou do prazo de {seconds:g} s")
                                    outgoing.extend(
                                        encode(
                                            {
                                                "type": "resource",
                                                "url": response.url,
                                                "headers": dict(response.headers),
                                                "status": response.status,
                                                "body": base64.b64encode(body).decode("ascii"),
                                            }
                                        )
                                    )
                                elif kind == "pdf" and started:
                                    if time.monotonic() >= deadline:
                                        raise RenderLimit(f"passou do prazo de {seconds:g} s")
                                    content = base64.b64decode(message["content"], validate=True)
                                    # O PDF inteiro já foi drenado: só agora é seguro esperar a
                                    # saída e observar também erros tardios da limpeza do motor.
                                    try:
                                        code = process.wait(
                                            timeout=max(0, deadline - time.monotonic())
                                        )
                                    except subprocess.TimeoutExpired:
                                        raise RenderLimit(
                                            f"passou do prazo de {seconds:g} s"
                                        ) from None
                                    if code:
                                        _process_failure(code, memory)
                                    return content, int(message["pages"])
                                else:
                                    raise RenderProcessError("resposta inválida do motor PDF")
        except (BrokenPipeError, ConnectionResetError):
            _process_failure(process.wait(timeout=0.5), memory)
        finally:
            _stop(process)
