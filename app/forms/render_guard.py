"""Prazo e teto opcional de memória de um trecho Python, usado pelos templates Jinja.

O Jinja roda na thread de quem pede (requisição ou task). Uma thread vigia relógio e memória;
passando do limite, agenda `RenderAborted` com `PyThreadState_SetAsyncExc`. O guarda traduz a
interrupção em `RenderLimit`, com mensagem fixa, e encerra a vigia antes de devolver o controle.
Uma chamada longa de C só recebe a interrupção ao retornar ao interpretador.

A interrupção assíncrona também pode atingir callbacks de limpeza que não propagam exceções.
Por isso o motor PDF não usa este guarda: `app.adapters.pdf_process` o executa num processo
exec descartável e corta o processo inteiro, preservando a thread solicitante e o próximo PDF.

O teto opcional mede crescimento residente do processo inteiro, incluindo outras threads.
No Linux lê `/proc/self/statm`; no macOS usa o pico residente, que não diminui. Templates pedem
apenas prazo, para alocações de outras requisições não interromperem uma renderização inocente.

A parada da vigia começa por uma atribuição só (`stop[0] = True`), sem chamada nem salto, e a
limpeza se repete se chegar uma interrupção tardia. Uma vigia não pode sobreviver ao guarda:
as threads solicitantes pertencem a pools e depois atendem outra pessoa.
"""

from __future__ import annotations

import ctypes
import os
import resource
import sys
import threading
import time
from collections.abc import Callable
from typing import TypeVar

_T = TypeVar("_T")

# De quanto em quanto tempo a thread vigia olha o relógio e a memória.
POLL_SECONDS = 0.05


class RenderAborted(BaseException):
    """Entra na thread que renderiza quando o prazo ou o teto de memória estoura. É `BaseException`
    de propósito: o `except Exception` de dentro do Jinja ou do WeasyPrint não a engole. Quem chama
    `run_within_limits` nunca a vê: ela sai como `RenderLimit`."""


class RenderLimit(Exception):
    """A renderização passou do prazo ou do teto de memória. A mensagem é fixa, sem dado da lead."""


def _resident_bytes() -> int:
    """A memória residente do processo agora (Linux) ou o pico dela (macOS e outros)."""
    try:
        with open("/proc/self/statm", "rb") as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform == "darwin" else peak * 1024  # o macOS dá bytes; o resto, KiB


def _interrupt(thread_id: int, error: type[BaseException] | None) -> None:
    """Agenda `error` na thread (ou, com None, desfaz o que estava agendado e ainda não chegou)."""
    pending = ctypes.py_object(error) if error is not None else None  # None é o ponteiro nulo
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), pending)


def run_within_limits(call: Callable[[], _T], *, seconds: float, memory: int | None = None) -> _T:
    """Roda `call()` e devolve o resultado, ou levanta `RenderLimit` se ele passar de `seconds`
    segundos ou, quando `memory` vem, fizer a memória do processo crescer mais que `memory` bytes.
    Outro erro de `call` segue como está."""
    thread_id = threading.get_ident()
    stop = [False]  # quem manda a thread vigia parar; `finished` só a acorda antes dos 50 ms
    finished = threading.Event()
    exceeded: list[RenderLimit] = []
    started = time.monotonic()
    baseline = _resident_bytes() if memory is not None else 0

    def watch() -> None:
        while not finished.wait(POLL_SECONDS):
            if stop[0]:
                return
            if not exceeded:
                if time.monotonic() - started > seconds:
                    exceeded.append(RenderLimit(f"passou do prazo de {seconds:g} s"))
                elif memory is not None and _resident_bytes() - baseline > memory:
                    megabytes = memory // 2**20
                    exceeded.append(RenderLimit(f"passou de {megabytes} MB de memória"))
            if (
                exceeded and not stop[0]
            ):  # repete até sair: um `except` largo de dentro pode ter engolido
                _interrupt(thread_id, RenderAborted)

    watcher = threading.Thread(target=watch, name="render-guard", daemon=True)
    # Tudo, até o `start()` da vigia, fica dentro dos `try`: ela pode cortar antes de o `start()`
    # voltar (o prazo é curto, ou a thread ficou parada), e a interrupção, até no meio da limpeza, é
    # pega pelo de fora e nunca vaza para o código de quem chamou.
    try:
        try:
            watcher.start()
            return call()
        finally:
            stop[0] = True  # uma atribuição só: sem chamada nem salto, não é interrompida no meio
            while True:
                try:
                    finished.set()
                    if watcher.ident is not None:  # sem ele, o `start()` nem chegou a criá-la
                        watcher.join()
                    _interrupt(thread_id, None)  # uma interrupção agendada e que não chegou
                    break
                except RenderAborted:
                    continue  # chegou tarde: refaz a limpeza até o fim
    except RenderAborted as aborted:
        if not exceeded:  # de uma guarda de fora, que também vigia esta thread
            raise
        raise exceeded[0].with_traceback(aborted.__traceback__) from None
