"""Disparo oportunista após commit; jobs persistidos continuam sendo a outbox recuperável."""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def launch_after_commit(app: FastAPI, job_id: uuid.UUID, attempt: int) -> None:
    """Inicia sem prender a resposta. Falha/interrupção deixa o reconciliador retomar o job."""

    async def launch() -> None:
        try:
            await app.state.trigger(job_id, attempt)
        except Exception:  # noqa: BLE001 — fronteira do trigger; o commit já vale e a outbox recupera.
            # Não registrar mensagem bruta do provedor nem dados que a exceção possa conter.
            logger.warning("disparo adiado para reconciliação job=%s attempt=%s", job_id, attempt)

    task = asyncio.create_task(launch())
    app.state.submission_launches.add(task)
    task.add_done_callback(app.state.submission_launches.discard)


async def stop_launches(app: FastAPI) -> None:
    """No encerramento, não esperar um provedor lento: a intenção já está no banco."""
    tasks = tuple(app.state.submission_launches)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def launch_capture_after_commit(app: FastAPI, submission_id: uuid.UUID) -> None:
    """Dispara somente IDs persistidos; SDK e provedor não prendem o HTTP da participante."""

    async def launch() -> None:
        from app.admin.launching import launch_capture_later

        try:
            await asyncio.wrap_future(launch_capture_later(submission_id))
        except Exception:  # noqa: BLE001 — falha isolada, sem mensagem bruta.
            logger.warning("disparo de captura adiado submission=%s", submission_id)

    task = asyncio.create_task(launch())
    app.state.submission_launches.add(task)
    task.add_done_callback(app.state.submission_launches.discard)
