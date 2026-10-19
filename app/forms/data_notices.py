"""O aviso dos dados vencidos de um formulário do banco (issue #48).

Quando uma janela de datas dos dados de um formulário (bloco `windows`, como a janela de opção do
reforma-2027) termina, o prompt passa a dizer que ela fechou e as leads recebem orientação sobre uma
base que ninguém conferiu. A regra é declarativa e vale para qualquer formulário que tenha um bloco
de janelas, sem código por formulário:

- **janela terminada:** o dia de hoje, no fuso da lead, é depois do fim de uma janela. Há um aviso
  por bloco, que cita o bloco, as janelas terminadas e as datas, e diz quando nenhuma janela do
  bloco tem mais data futura;
- **versão velha:** a versão atual, que é onde as datas vivem (elas só mudam publicando), foi
  publicada há mais de `REVIEW_MAX_AGE_DAYS` dias. Só vale para quem tem um bloco de janelas: um
  formulário sem datas não fica velho por ficar um mês sem mudar.

O aviso aparece na lista e na leitura do formulário no admin e no log da subida da API e do worker
(`log_data_notices`). Nada aqui recusa um envio, e o texto cita só nomes e datas dos dados do
formulário, nunca dado da lead.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.forms import stored as stored_forms
from app.forms.dates import local_date, short_date
from app.forms.definition.document import WindowsBlock

logger = logging.getLogger(__name__)

# Quantos dias a versão atual pode ter desde a publicação, num formulário com janelas de datas, sem
# o aviso de revisão. Uma referência datada pode mudar entre uma campanha
# e outra: um mês é o quanto a operadora aceita ficar sem conferir a fonte (a mesma regra do PR #34).
REVIEW_MAX_AGE_DAYS = 30


@dataclass(frozen=True)
class DataNotice:
    """Um aviso sobre os dados de um formulário: qual (`slug` e `title`) e o que conferir. A
    `message` não repete o nome do formulário: a tela e o log o põem antes dela."""

    slug: str
    title: str
    message: str


def _join(parts: list[str]) -> str:
    """ "a", "a e b", "a, b e c"."""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} e {parts[-1]}"


def _windows_blocks(definition: Mapping[str, Any]) -> dict[str, WindowsBlock]:
    """Os blocos de janelas dos dados da definição, pelo nome, em ordem de nome. Um bloco que não
    é de janelas, ou que não confere com o formato, fica de fora: o aviso não derruba nada."""
    data = definition.get("data")
    if not isinstance(data, Mapping):
        return {}
    blocks: dict[str, WindowsBlock] = {}
    for name in sorted(data):
        raw = data[name]
        if not isinstance(raw, Mapping) or raw.get("kind") != "windows":
            continue
        try:
            blocks[name] = WindowsBlock.model_validate(raw)
        except ValidationError:  # sem o valor recebido: só o formato importa aqui
            continue
    return blocks


def _ended_message(name: str, block: WindowsBlock, today: date) -> str | None:
    """O aviso das janelas do bloco que terminaram antes de `today`; None se nenhuma terminou. O
    último dia da janela ainda conta como aberto."""
    ended = [window for window in block.windows if window.end < today]
    if not ended:
        return None
    items = [
        f"a janela «{window.label or window.name}» (em {short_date(window.end)})"
        for window in ended
    ]
    verb = "já terminou" if len(ended) == 1 else "já terminaram"
    message = f"No bloco «{name}», {verb} {_join(items)}."
    if len(ended) == len(block.windows):
        message += " Nenhuma janela do bloco tem mais data futura."
    return f"{message} Confira a fonte e, se as datas mudaram, publique uma versão nova."


def _review_message(names: list[str], published_at: datetime, today: date) -> str | None:
    """O aviso da versão publicada há mais de `REVIEW_MAX_AGE_DAYS` dias (em dias de calendário, no
    fuso da lead); None se ela é recente."""
    age = (today - local_date(published_at)).days
    if age <= REVIEW_MAX_AGE_DAYS:
        return None
    blocks = [f"«{name}»" for name in names]
    where = f"do bloco {blocks[0]}" if len(blocks) == 1 else f"dos blocos {_join(blocks)}"
    return (
        f"A versão atual foi publicada em {short_date(published_at)}, há {age} dias: confira se "
        f"as datas {where} ainda valem."
    )


def data_notices(
    slug: str, definition: Mapping[str, Any], *, published_at: datetime, now: datetime
) -> list[DataNotice]:
    """Os avisos da versão atual de um formulário (a `definition` como o banco a guarda, publicada em
    `published_at`) no dia de `now`, que vale no fuso da lead: primeiro as janelas terminadas, por
    nome de bloco, depois a revisão. Função pura: sem banco e sem relógio."""
    today = local_date(now)
    title = str(definition.get("title") or slug)
    blocks = _windows_blocks(definition)
    messages = [_ended_message(name, block, today) for name, block in blocks.items()]
    if blocks:
        messages.append(_review_message(list(blocks), published_at, today))
    return [DataNotice(slug, title, message) for message in messages if message]


def current_notices(session: Session, now: datetime) -> dict[str, list[DataNotice]]:
    """Os avisos da versão atual de cada formulário do banco no dia de `now`, pelo slug; só entram
    os formulários com aviso. Um formulário inativo também entra: ele guarda as mesmas datas, e a
    operadora o liga sem passar por uma publicação."""
    found: dict[str, list[DataNotice]] = {}
    for version in stored_forms.current_versions(session):
        notices = data_notices(
            version.slug, version.definition, published_at=version.published_at, now=now
        )
        if notices:
            found[version.slug] = notices
    return found


def log_data_notices(sessions: sessionmaker[Session], now: datetime | None = None) -> int:
    """Escreve no log, em WARNING, cada aviso dos formulários do banco no dia de `now` (o agora, se
    não vier), e devolve quantos foram. Só o slug e o texto do aviso vão ao log. Levanta o erro do
    banco (fora do ar, ou sem as tabelas antes das migrations): quem sobe decide o que fazer."""
    with sessions() as session:
        found = current_notices(session, now or datetime.now(UTC))
    count = 0
    for slug, notices in found.items():
        for notice in notices:
            logger.warning("formulário %s: %s", slug, notice.message)
            count += 1
    return count
