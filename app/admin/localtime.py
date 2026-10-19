"""O fuso do admin: Brasília, que não tem horário de verão desde 2019.

A página mostra as datas em Brasília e o filtro "Recebido" conta dias de calendário de Brasília; o
banco e a API guardam e dizem os instantes em UTC. Aqui mora o fuso e a conta do dia, para a
consulta (`app.admin.queries.jobs`) e a página (`app.admin.views`) usarem o mesmo.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    LOCAL_TZ: tzinfo = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:  # imagem sem tzdata: -03:00 fixo, que é o que Brasília usa hoje
    LOCAL_TZ = timezone(timedelta(hours=-3), "BRT")


def local_midnight(now: datetime, *, days_back: int = 0) -> datetime:
    """A meia-noite de Brasília, `days_back` dias de calendário antes do dia de `now`: o primeiro
    instante daquele dia. `now` leva fuso (qualquer um): o dia é o de Brasília naquele instante, e
    não o de UTC (às 22:00 de Brasília já é o dia seguinte em UTC)."""
    day = now.astimezone(LOCAL_TZ).date() - timedelta(days=days_back)
    return datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
