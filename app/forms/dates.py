"""Datas em português, no fuso da lead (São Paulo): no PDF, no prompt e nos filtros dos templates
de uma definição."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

LEAD_TIMEZONE = ZoneInfo("America/Sao_Paulo")
MONTHS = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)


def local_date(value: date | datetime) -> date:
    """O dia no fuso da lead: um `datetime` muda de fuso; um `date` já é o dia."""
    return value.astimezone(LEAD_TIMEZONE).date() if isinstance(value, datetime) else value


def long_date(value: date | datetime) -> str:
    """Data por extenso no fuso da lead, ex.: "7 de outubro de 2026" e "1º de janeiro de 2027"."""
    day = local_date(value)
    return f"{'1º' if day.day == 1 else day.day} de {MONTHS[day.month - 1]} de {day.year}"


def short_date(value: date | datetime) -> str:
    """Data curta no fuso da lead, ex.: "07/10/2026"."""
    return local_date(value).strftime("%d/%m/%Y")


def month_year(value: date | datetime) -> str:
    """Mês e ano no fuso da lead, ex.: "outubro de 2026"."""
    day = local_date(value)
    return f"{MONTHS[day.month - 1]} de {day.year}"


def _day_month(day: date) -> str:
    return f"{'1º' if day.day == 1 else day.day} de {MONTHS[day.month - 1]}"


def date_range(first: date, last: date) -> str:
    """O período por extenso, ex.: "8 de outubro a 6 de novembro de 2026". O ano aparece nas duas
    pontas quando o período atravessa a virada: "20 de dezembro de 2026 a 18 de janeiro de 2027"."""
    head = _day_month(first) + ("" if first.year == last.year else f" de {first.year}")
    return f"{head} a {_day_month(last)} de {last.year}"
