"""Ponta a ponta com o Hatchet de verdade: API (em processo) -> Hatchet Cloud -> worker
(subprocesso) -> Postgres de teste -> webhook local.

Roda só com `HATCHET_E2E=1` e um token real em `HATCHET_CLIENT_TOKEN` ou no arquivo
`.env.hatchet` na raiz do projeto. Consome algumas execuções da cota do tenant. O PDF é sempre
real (WeasyPrint). Com `E2E_REAL_LLM=1` o plano vem da OpenAI (`OPENAI_API_KEY` no ambiente,
modelo em `E2E_OPENAI_MODEL`, padrão `gpt-5.4-mini`), com custo de uma chamada.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import DEMO_ANSWERS, DEMO_CONSENT
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.deps import Deps

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("HATCHET_E2E") != "1", reason="defina HATCHET_E2E=1"),
    # Em paralelo (`-n N --dist loadgroup`), os e2e vão para o mesmo worker: o tenant do Hatchet
    # Cloud é um só, e o worker de um teste pegaria as tasks de outro, que usa outro banco. Hoje há um
    # teste só; o grupo vale para os próximos. O banco, os arquivos e as portas já são por worker ou
    # por teste.
    pytest.mark.xdist_group("hatchet-e2e"),
]

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_SECRET = "segredo-do-webhook-de-teste-0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Hook:
    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, str], bytes]] = []
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                hook.requests.append((dict(self.headers), body))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def hook() -> Iterator[Hook]:
    h = Hook()
    yield h
    h.server.shutdown()


@pytest.fixture
def worker(deps: Deps, hook: Hook, tmp_path: Path) -> Iterator[None]:
    health_port = free_port()
    env = {
        **os.environ,
        "LOCAL_STORAGE_DIR": str(deps.storage.root),  # type: ignore[attr-defined]
        "WEBHOOK_URL": hook.url,
        "WEBHOOK_SECRET": WEBHOOK_SECRET,
        "RECONCILE_CRON": "",
        "WORKER_SLOTS": "4",
        "PDF_MODE": "weasyprint",
        "HATCHET_CLIENT_WORKER_HEALTHCHECK_ENABLED": "True",
        "HATCHET_CLIENT_WORKER_HEALTHCHECK_PORT": str(health_port),
        "HATCHET_CLIENT_WORKER_HEALTHCHECK_BIND_ADDRESS": "127.0.0.1",
    }
    if os.environ.get("E2E_REAL_LLM") == "1":
        env |= {
            "LLM_MODE": "openai",
            "OPENAI_MODEL": os.environ.get("E2E_OPENAI_MODEL", "gpt-5.4-mini"),
        }
    log = (tmp_path / "worker.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"worker saiu: {(tmp_path / 'worker.log').read_text()[-2000:]}")
            try:
                if (
                    httpx.get(f"http://127.0.0.1:{health_port}/health", timeout=1).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            pytest.fail("worker não ficou saudável em 60 s")
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


def test_submission_to_pdf_and_webhook_through_hatchet(
    deps: Deps, hook: Hook, worker: None
) -> None:
    from app.api.main import create_app

    client = TestClient(create_app())
    response = client.post(
        "/v1/forms/demo/submissions",
        json={"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT, "tracking": {"utm_source": "e2e"}},
        headers={"Idempotency-Key": f"e2e-{time.time()}"},
    )
    assert response.status_code == 202, response.text
    public_id, token = response.json()["public_id"], response.json()["token"]

    status: dict[str, Any] = {}
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status = client.get(f"/v1/jobs/{public_id}", params={"t": token}).json()
        if status["status"] in ("completed", "failed"):
            break
        time.sleep(2)
    assert status.get("status") == "completed", status

    pdf = client.get(f"/v1/jobs/{public_id}/pdf", params={"t": token})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    text = " ".join(
        " ".join(page.extract_text().split()) for page in PdfReader(BytesIO(pdf.content)).pages
    )
    assert DEMO_ANSWERS["nome"] in text  # PDF real do template, com o nome do lead

    deadline = time.monotonic() + 60
    while not hook.requests and time.monotonic() < deadline:
        time.sleep(1)
    assert hook.requests, "webhook não chegou"
    headers, body = hook.requests[0]
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        headers["X-MCC-Timestamp"].encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-MCC-Signature"] == f"v1={expected}"
    payload = json.loads(body)
    assert payload["event"] == "plano.concluido" and payload["job"]["public_id"] == public_id
    assert payload["tracking"] == {"utm_source": "e2e"} and payload["pdf"]["url"].endswith(token)
    assert " ".join(payload["plan"]["titulo"].split()) in text  # o plano entregue é o do PDF
