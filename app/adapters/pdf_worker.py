"""Um único motor PDF em processo exec, sem clientes de storage, banco ou fila."""

from __future__ import annotations

import base64
import os
import resource
import socket
import sys
import threading
from pathlib import Path
from typing import Any

from app.adapters.pdf_ipc import encode, receive

MEMORY_EXIT = 74
UNRAISABLE_EXIT = 75


def _resident_bytes() -> int:
    if sys.platform == "linux":
        with open("/proc/self/statm", "rb") as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def main(channel: socket.socket) -> None:
    # Não há injeção de exceções. Uma falha de limpeza genuína continua sendo falha do PDF,
    # sem imprimir o objeto ou a exceção que poderiam conter dados da participante.
    sys.unraisablehook = lambda event: os._exit(UNRAISABLE_EXIT)
    from weasyprint import CSS, HTML
    from weasyprint.text.fonts import FontConfiguration
    from weasyprint.urls import URLFetcherResponse

    channel.sendall(encode({"type": "ready"}))
    job = receive(channel)
    baseline = _resident_bytes()
    finished = threading.Event()

    def watch_memory() -> None:
        while not finished.wait(0.05):
            if _resident_bytes() - baseline > job["memory"]:
                os._exit(MEMORY_EXIT)

    class Fetcher:
        _fail_on_errors = True

        def __call__(self, url: str) -> URLFetcherResponse:
            channel.sendall(encode({"type": "fetch", "url": url}))
            response = receive(channel)
            return URLFetcherResponse(
                response["url"],
                base64.b64decode(response["body"], validate=True),
                response["headers"],
                response["status"],
            )

    watcher = threading.Thread(target=watch_memory, name="pdf-memory", daemon=True)
    watcher.start()
    try:
        fonts = FontConfiguration()
        fetcher = Fetcher()
        options: dict[str, Any] = {"base_url": job["base_url"], "url_fetcher": fetcher}
        stylesheets = [
            CSS(
                string=job["fonts_css"],
                font_config=fonts,
                url_fetcher=fetcher,
                base_url=str(Path(job["base_url"]) / "fonts" / "fonts.css"),
            )
        ]
        if job["css"].strip():
            stylesheets.append(CSS(string=job["css"], font_config=fonts, **options))
        rendered = HTML(string=job["html"], **options).render(
            stylesheets=stylesheets, font_config=fonts
        )
        content = rendered.write_pdf()
        channel.sendall(
            encode(
                {
                    "type": "pdf",
                    "content": base64.b64encode(content).decode("ascii"),
                    "pages": len(rendered.pages),
                }
            )
        )
    except Exception:  # noqa: BLE001 - o protocolo não transporta mensagens do motor
        channel.sendall(encode({"type": "error"}))
    finally:
        finished.set()
        watcher.join()


if __name__ == "__main__":
    with socket.socket(fileno=int(sys.argv[1])) as channel:
        main(channel)
