"""Quanto tempo o passo `plan` do Hatchet tem para falar com a OpenAI.

O número mora aqui, num lugar só, porque três pontos dependem dele: a task `plan`
(`app/workflows.py`) é cancelada em `PLAN_EXECUTION_TIMEOUT`; o adaptador da OpenAI
(`app/adapters/openai_llm.py`) faz uma rodada de reparo por modelo (`MAX_REPAIRS`); e o
`Settings.check()` recusa um `OPENAI_TIMEOUT_SECONDS` que, no pior caso, estoura a task. Com o
timeout passando do limite, o Hatchet cancela a task no meio da chamada, a resposta paga se perde e
cada nova tentativa repete o custo.
"""

from __future__ import annotations

from datetime import timedelta

# Rodadas de reparo depois do pedido original, em cada modelo: o plano inválido volta ao modelo uma
# vez com os erros de validação.
MAX_REPAIRS = 1
# O `execution_timeout` da task `plan`. Se mudar, o `Settings.check()` passa a recusar os timeouts
# da OpenAI que não cabem mais.
PLAN_EXECUTION_TIMEOUT = timedelta(minutes=9)
# O que a task gasta além das chamadas: banco, validação do plano e a abertura da conexão.
PLAN_MARGIN = timedelta(minutes=1)


def llm_calls(*, fallback: bool) -> int:
    """O máximo de chamadas à OpenAI numa tentativa da task: (modelo principal e, se houver,
    o de reserva) x (pedido + rodadas de reparo)."""
    return (2 if fallback else 1) * (MAX_REPAIRS + 1)


def worst_case(call_timeout_seconds: float, *, fallback: bool) -> timedelta:
    """O pior caso do tempo no LLM numa tentativa da task: todas as chamadas levando o timeout."""
    return timedelta(seconds=call_timeout_seconds) * llm_calls(fallback=fallback)
