"""Prazo, e quando se pede teto de memória, para renderizar um template ou um PDF.

O Jinja e o WeasyPrint rodam na thread de quem pede (uma requisição, uma task do worker) e, sozinhos,
não têm prazo nem teto: um template feito para pesar (laços aninhados sem saída, um texto que dobra a
cada volta) ou um HTML de 800 bytes que vira cem mil parágrafos prende a thread, e a do PDF ainda segura
o lock de renderização, então todo PDF do processo espera.

`run_within_limits` roda um trecho com o prazo e, se pedido, o teto de memória. Uma thread vigia o
relógio e a memória do processo e, passando de um dos dois, interrompe a thread que renderiza
(`PyThreadState_SetAsyncExc`): o trecho termina com `RenderLimit`, de mensagem fixa, sem nenhum dado da
lead. A interrupção chega entre dois bytecodes, e o Jinja e quase todo o WeasyPrint são Python, então
ela pega o laço sem fim e a renderização lenta; uma única chamada de C longa (a multiplicação de dois
números enormes, uma linha de texto que o Pango demora a quebrar) termina antes da interrupção chegar.

O teto de memória é o **crescimento** da memória residente do processo desde o começo do trecho, não o
que o trecho aloca sozinho: outra thread que alocar muito ao mesmo tempo (o otimizador de imagens, com
até 40 MP) também conta, e a tiraria de uma renderização inocente. Por isso só quem roda sozinho, com o
lock de renderização, o pede (o PDF); o template, que roda em qualquer thread do servidor, fica só com
o prazo. No Linux a leitura é a memória residente de agora (`/proc/self/statm`); no macOS e nos outros,
o pico dela, que só sobe, e então só se vê crescimento acima do maior pico anterior do processo. O teto
não impede uma alocação enorme de uma vez só (ela já aconteceu quando a thread vigia): corta a que cresce
aos poucos, que é a que satura o servidor.

A interrupção pode chegar tarde, até no meio da limpeza do próprio guarda, e a thread vigia nunca pode
sobrar: ela tiraria o prazo de uma renderização de outra pessoa, porque o guarda roda em threads de um
pool. Por isso a parada dela começa por uma atribuição só (`stop[0] = True`), sem chamada nem salto,
onde o interpretador não entrega interrupções, e o resto da limpeza se repete até terminar.
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
