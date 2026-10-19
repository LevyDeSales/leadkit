"""Página do pedido: do estado do job (`jobs.status`/`jobs.stage`) para a linguagem do lead.

Só mostramos etapas que o pipeline registra de verdade (estado honesto): o job entra na fila,
gera o plano (`generating`) e o PDF (`rendering`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import Job

BRASILIA = timezone(timedelta(hours=-3))  # sem horário de verão desde 2019; o lead não vê UTC
STEPS = ("Recebemos suas respostas", "Montando seu plano de ação", "Gerando o seu PDF")
# stage do job -> (etapa em andamento, observação)
CURRENT = {
    "queued": (1, "Começa em instantes."),
    "generating": (1, "É a etapa mais longa."),
    "rendering": (2, None),
}
SLOW_AFTER = timedelta(minutes=4)  # provisório: recalibrar com o p90 medido


@dataclass(frozen=True)
class StepState:
    label: str
    state: str  # done | current | todo
    note: str | None = None


@dataclass(frozen=True)
class Progress:
    stage: str
    steps: tuple[StepState, ...]
    current: int
    slow: bool
    poll_seconds: int

    @property
    def announcement(self) -> str:
        label = self.steps[self.current].label
        return f"Etapa {self.current + 1} de {len(self.steps)}: {label[:1].lower()}{label[1:]}."


def progress(job: Job, now: datetime) -> Progress:
    """Sem novidade há `SLOW_AFTER` = lento. O polling desacelera enquanto nada muda
    (3 s no primeiro minuto, 5 s até 5 min, depois 10 s)."""
    current, note = CURRENT.get(job.stage, CURRENT["queued"])
    steps = tuple(
        StepState(
            label,
            "done" if index < current else "current" if index == current else "todo",
            note if index == current else None,
        )
        for index, label in enumerate(STEPS)
    )
    quiet = now - job.updated_at
    poll = 3 if quiet < timedelta(minutes=1) else 5 if quiet < timedelta(minutes=5) else 10
    return Progress(job.stage, steps, current, quiet > SLOW_AFTER, poll)


def date_br(moment: datetime) -> str:
    return moment.astimezone(BRASILIA).strftime("%d/%m/%Y")


def file_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.1f} MB".replace(".", ",")
