"""As ações do admin sobre a integração de um formulário com uma tabela do Baserow: conectar o
formulário a uma tabela (`connect`), ler os campos da tabela como o Baserow os descreve agora
(`read_fields`), ler a integração inteira (`read_detail`: a conexão, os campos de agora com a fonte
de cada um, o relatório e as sincronizações), guardar o mapeamento (`save_mapping`), ativar
(`activate`, só com o relatório limpo contra a tabela de agora), desativar (`deactivate`), remover
(`remove`), mandar a linha de teste (`send_test`) e reenviar em lote as sincronizações que falharam
(`resend_failed`).

A página e a API chamam as mesmas funções (ADR 0015, "A página e a API dividem os serviços"): elas não
conhecem `Request`, HTML nem aviso de tela. Recebem o autor (`actor`: `ADMIN_USERNAME` pela página,
`chave:<nome>` pela API), devolvem um resultado ou levantam uma exceção de domínio, e cada lado a
traduz (a página, em aviso e redirecionamento; a API, em JSON e status). As conferências da conexão
(o endereço, o número da tabela, o token), a leitura de teste da tabela, a leitura dos campos de agora,
a decisão de desativar a integração quando o mapeamento novo tem pendências e a conferência do
relatório antes de ativar moravam dentro das rotas HTML; moveram-se para cá sem mudar.

**A versão esperada se compara antes de falar com o Baserow**, em `connect`, `save_mapping` e
`activate`: quem viu uma versão velha, ou uma integração que sumiu, recebe o conflito, e não o erro do
Baserow que estiver fora do ar. A palavra final é do `integration_config`, sob o lock.

**O token só entra.** Ele chega aqui em texto, vai ao Baserow e ao `integration_config.connect` (que o
cifra) e não sai: nem na exceção, nem no log, nem na auditoria. As mensagens de recusa citam o status
e o código de erro do Baserow (`app.integrations.baserow`), nunca o token nem o corpo da resposta.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from app import destinations
from app.admin import launching
from app.admin.queries import forms as form_queries
from app.admin.queries import integrations as queries
from app.deps import Deps
from app.errors import RetryableError, TerminalError
from app.integrations import config as integration_config
from app.integrations import mapping as integration_mapping
from app.integrations import push as integration_push
from app.integrations import syncs as integration_syncs
from app.integrations.baserow import BaserowField, Connection

logger = logging.getLogger(__name__)

MAX_NUMBER = 2**31 - 1  # o maior inteiro (de 32 bits) que o banco e o Baserow guardam
MAX_TABLE_ID = MAX_NUMBER
MAX_NUMBER_LENGTH = len(str(MAX_NUMBER))  # 10 algarismos: um texto maior nunca é um número válido
NOT_PUBLISHED = (
    "Publique o formulário antes: o mapeamento parte das perguntas da versão publicada, e ainda "
    "não há nenhuma."
)
INTEGRATION_MISSING = "Este formulário ainda não tem integração: conecte o Baserow primeiro."
TOKEN_REQUIRED = "Cole o token de banco de dados do Baserow."
TABLE_INVALID = (
    "Informe o número da tabela: é o que vem depois de /table/ no endereço dela no Baserow."
)
TOKEN_UNREADABLE = (
    "O token guardado não abre com a WEBHOOK_ENCRYPTION_KEY atual: cole o token de novo."
)
NEW_SERVER_NEEDS_TOKEN = (
    "Mudar o servidor do Baserow (o host, a porta ou o https) exige colar o token de novo: o token "
    "guardado nunca vai a outro servidor."
)
NO_CIPHER = (
    "Para guardar o token do Baserow, defina WEBHOOK_ENCRYPTION_KEY no ambiente do app e reinicie "
    "a API e o worker (como gerar a chave: app/README.md)."
)
UNMATCHED_OPTIONS = (
    "{count} {what} par no Baserow. Acrescente-as no campo da tabela com o mesmo texto que a lead "
    "vê, ou ajuste o texto da opção no formulário."
)


class InvalidConnection(Exception):
    """A conexão não vale e nada foi lido nem gravado. `errors` mapeia o campo (`base_url`,
    `table_id`, `token`) ao motivo, em português. Os motivos dos campos saem juntos. O token digitado
    nunca entra na mensagem."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("conexão recusada: " + ", ".join(errors))
        self.errors = errors


class NotPublished(Exception):
    """O formulário ainda não tem versão publicada: o mapeamento parte das perguntas dela."""

    def __init__(self) -> None:
        super().__init__(NOT_PUBLISHED)


class IntegrationMissing(Exception):
    """O formulário ainda não tem integração: conecte o Baserow primeiro."""

    def __init__(self) -> None:
        super().__init__(INTEGRATION_MISSING)


class ReadFailed(Exception):
    """Não deu para ler a tabela de agora. A mensagem é para a tela, em português, e nunca traz o
    token."""


class TokenUnreadable(ReadFailed):
    """O token guardado não abre com a `WEBHOOK_ENCRYPTION_KEY` atual: ninguém o lê até que alguém
    cole o token de novo. O Baserow nem foi chamado."""

    def __init__(self) -> None:
        super().__init__(TOKEN_UNREADABLE)


class TableNotRead(ReadFailed):
    """O Baserow não leu a tabela com o token (não respondeu, recusou o token ou não achou a
    tabela). A mensagem é o status e o código de erro do Baserow, nunca o token."""


class ReportPending(Exception):
    """O mapeamento guardado, conferido contra a tabela de agora, tem pendências (ou nada foi mapeado):
    a integração só liga com o relatório limpo. `nothing_mapped` diz que o relatório está sujo só porque
    não há mapeamento. Nada foi gravado."""

    def __init__(self, report: integration_mapping.Report, *, nothing_mapped: bool) -> None:
        super().__init__("o relatório do mapeamento tem pendências")
        self.report = report
        self.nothing_mapped = nothing_mapped

    def reasons(self) -> list[str]:
        """O que corrigir, em frases: os problemas do relatório e, se há, as opções do formulário que o
        campo de seleção do Baserow não tem. Nenhuma traz dado de lead."""
        found = list(self.report.problems)
        count = self.report.unmatched
        if count:
            what = "opção do formulário não tem" if count == 1 else "opções do formulário não têm"
            found.append(UNMATCHED_OPTIONS.format(count=count, what=what))
        return found


@dataclass(frozen=True)
class Connected:
    """A conexão guardada. `created`: a integração não existia (a versão esperada era 0)."""

    integration: integration_config.StoredIntegration
    created: bool


@dataclass(frozen=True)
class MappingSaved:
    """O mapeamento guardado. `report` é o do mapeamento novo contra a tabela de agora e
    `was_active` diz se a integração estava ativa antes: com pendências no relatório, ela foi
    desativada (`deactivated`)."""

    integration: integration_config.StoredIntegration
    report: integration_mapping.Report
    was_active: bool

    @property
    def deactivated(self) -> bool:
        return self.was_active and not self.report.ok


@dataclass(frozen=True)
class Removed:
    """A integração que saiu, como estava, e o estado das sincronizações que saíram com ela. As linhas
    que já estavam na tabela do Baserow ficam lá: o app não apaga nada no Baserow."""

    integration: integration_config.StoredIntegration
    sync_counts: integration_syncs.SyncCounts


@dataclass(frozen=True)
class FailedResent:
    """As sincronizações que falharam e voltaram para a fila (`sync_ids`, as mais antigas primeiro, até
    `launching.RESEND_FAILED_LIMIT`), e quantas continuam falhadas (`still_failed`: as que passaram do
    limite). Quem chama dispara as `sync_ids` depois."""

    sync_ids: list[uuid.UUID]
    still_failed: int


def _number(text: str) -> int | None:
    """O inteiro de um texto só de algarismos ASCII com no máximo `MAX_NUMBER_LENGTH` deles (ou
    None). O tamanho é conferido **antes** do `int()`: com milhares de algarismos ele levanta
    `ValueError`, e o que vem de fora (um formulário, um corpo) não pode derrubar a chamada."""
    if text.isascii() and text.isdecimal() and len(text) <= MAX_NUMBER_LENGTH:
        return int(text)
    return None


def version_number(text: str) -> int:
    """A versão da integração que a pessoa viu, em texto opaco e canônico (`"2"`, e não `"002"`);
    o que não é uma versão que o servidor dá (vazio, com zero à esquerda, longo demais, não numérico)
    nunca bate: vale -1, e a ação responde `Conflict`."""
    number = _number(text)
    if number is None or number > MAX_NUMBER or (len(text) > 1 and text.startswith("0")):
        return -1
    return number


def table_number(text: str) -> int | None:
    """O número da tabela digitado (o que vem depois de `/table/` no endereço dela); None se não é
    um inteiro positivo que o banco guarda."""
    number = _number(text)
    return number if number is not None and 0 < number <= MAX_NUMBER else None


def field_number(text: str) -> int | None:
    """O id de um campo da tabela (a chave de `source_<id>` na página, de `sources` na API); None se
    não é um inteiro positivo que o Baserow guarda. A mesma regra do número da tabela."""
    return table_number(text)


def connect(
    deps: Deps,
    form_slug: str,
    *,
    base_url: str,
    table_id: int | None,
    token: str,
    expected_version: int,
    actor: str,
) -> Connected:
    """Conecta o formulário a uma tabela do Baserow, ou muda a conexão que ele tem.

    Confere o formulário (precisa de versão publicada: `NotPublished`, antes de tudo), o endereço
    (`check_base_url`), o número da
    tabela (`table_id` None é um número que não vale) e o token (obrigatório na criação,
    `expected_version` 0; em branco, numa integração que existe, mantém o guardado). Os motivos saem
    juntos em `InvalidConnection`. Só então lê os campos da tabela com o token: se o Baserow não a
    leu, `TableNotRead` e nada é gravado. Mudar o endereço ou a tabela apaga o mapeamento e
    desativa a integração.

    Levanta `InvalidConnection`, `TableNotRead` e `integration_config.Conflict` (a integração mudou
    desde que a pessoa a viu: nada muda; se a versão esperada não é 0 e a integração não existe mais,
    o conflito vem antes de qualquer leitura da tabela)."""
    production = deps.settings.app_env == "production"
    if integration_config.form_spec(deps, form_slug) is None:
        raise NotPublished  # o estado do formulário vem antes do corpo
    errors: dict[str, str] = {}
    address = ""
    try:
        address = integration_config.check_base_url(base_url, production=production)
    except integration_config.InvalidBaseUrl as exc:
        errors["base_url"] = str(exc)
    if table_id is None or not 0 < table_id <= MAX_TABLE_ID:
        errors["table_id"] = TABLE_INVALID
    token = token.strip()
    if expected_version == 0 and not token:
        errors["token"] = TOKEN_REQUIRED
    if errors or table_id is None:
        raise InvalidConnection(errors)
    # A versão esperada se compara no mesmo `load` que dá a atual, antes de qualquer leitura da tabela
    # (e do que o Baserow responder): quem viu uma versão velha, ou uma integração que sumiu (a versão
    # de uma integração que não existe é 0), recebe o conflito. A palavra final é do `connect` abaixo,
    # sob o lock, para o caso de a integração mudar entre uma coisa e outra.
    with deps.sessions() as db:
        current = integration_config.load(db, form_slug)
    if (current.version if current is not None else 0) != expected_version:
        raise integration_config.Conflict(current)
    if (
        current is not None
        and not token
        and integration_config.server_of(address) != integration_config.server_of(current.base_url)
    ):
        # O token guardado nunca vai a outro servidor: quem muda o endereço cola o token de novo.
        raise InvalidConnection({"token": NEW_SERVER_NEEDS_TOKEN})
    keep_token = not token  # em branco, numa integração que existe: segue o token guardado
    try:
        checked = token
        if keep_token:
            with deps.sessions() as db:
                kept = integration_config.stored_connection(db, deps.cipher, form_slug)
            checked = kept.token if kept else ""
        deps.baserow.fields(Connection(address, table_id, checked))
    except destinations.UnreadableSecret:
        raise InvalidConnection({"token": TOKEN_UNREADABLE}) from None
    except (RetryableError, TerminalError) as exc:
        raise TableNotRead(f"Não deu para ler a tabela: {exc.detail}.") from None
    try:
        with deps.sessions.begin() as db:
            saved = integration_config.connect(
                db,
                deps.cipher,
                form_slug=form_slug,
                base_url=address,
                table_id=table_id,
                token="" if keep_token else token,
                actor=actor,
                expected_version=expected_version,
                production=production,
            )
    except destinations.NoCipher:
        raise InvalidConnection({"token": NO_CIPHER}) from None
    logger.info(
        "admin: integração baserow %s formulario=%s host=%s tabela=%s por=%s",
        "conectada" if expected_version == 0 else "alterada",
        form_slug,
        saved.host,
        saved.table_id,
        actor,
    )
    return Connected(integration=saved, created=expected_version == 0)


def read_fields(deps: Deps, form_slug: str) -> list[BaserowField]:
    """Os campos da tabela como o Baserow os descreve agora, lidos com o token guardado: os tipos e as
    opções valem como estão lá, a cada chamada.

    Levanta `IntegrationMissing` (o formulário não tem integração), `TokenUnreadable` (o token
    guardado não abre; o Baserow nem é chamado) e `TableNotRead` (o Baserow não respondeu ou
    recusou)."""
    try:
        with deps.sessions() as db:
            connection = integration_config.stored_connection(db, deps.cipher, form_slug)
        if connection is None:
            raise IntegrationMissing
        return deps.baserow.fields(connection)
    except destinations.UnreadableSecret:
        raise TokenUnreadable from None
    except (RetryableError, TerminalError) as exc:
        raise TableNotRead(f"Não deu para ler a tabela: {exc.detail}.") from None


def read_detail(
    deps: Deps, form_slug: str, *, typed: Mapping[int, str] | None = None
) -> queries.IntegrationDetail | None:
    """A integração do formulário como a página e a API a leem: a conexão (nunca o token), os campos
    da tabela **lidos agora no Baserow** com a fonte de cada um, o relatório do mapeamento guardado e
    o estado das sincronizações. None se o formulário não existe.

    Uma tabela que não se leu (o Baserow fora do ar, o token que não abre) não derruba a leitura: os
    campos e o relatório ficam de fora e `read_error` diz por quê. Sem conexão, sem versão publicada
    ou com o token que não abre, o Baserow nem é chamado. `typed` são as escolhas que a pessoa acabou
    de mandar (numa recusa da página): valem no lugar das guardadas."""
    spec = integration_config.form_spec(deps, form_slug)
    with deps.sessions() as db:
        title = form_queries.form_title(db, form_slug)
        stored = integration_config.load(db, form_slug)
        unreadable = stored is not None and integration_config.token_unreadable(
            db, deps.cipher, form_slug
        )
    if title is None:
        return None
    sources = integration_mapping.catalog(spec)
    fields: list[queries.FieldChoice] | None = None
    report: integration_mapping.Report | None = None
    read_error = TOKEN_UNREADABLE if unreadable else None
    if stored is not None and spec is not None and not unreadable:
        try:
            live = read_fields(deps, form_slug)
        except (IntegrationMissing, ReadFailed) as exc:
            read_error = str(exc)
        else:
            fields = queries.field_choices(live, sources, stored.entries, typed)
            if stored.entries:
                report = integration_mapping.build_report(list(stored.entries), live, spec)
    with deps.sessions() as db:
        counts, failures = queries.sync_overview(db, stored)
    return queries.IntegrationDetail(
        slug=form_slug,
        title=title,
        published=spec is not None,
        stored=stored,
        token_unreadable=unreadable,
        fields=fields,
        read_error=read_error,
        report=report,
        sources=sources,
        counts=counts,
        failures=failures,
    )


def save_mapping(
    deps: Deps,
    form_slug: str,
    *,
    chosen: Mapping[int, str],
    expected_version: int,
    actor: str,
) -> MappingSaved:
    """Guarda o mapeamento: `chosen` é a fonte de cada campo da tabela (pelo id do campo no Baserow;
    uma fonte em branco, ou de um campo que a tabela não tem mais, não conta). É conferido contra a
    tabela como ela está agora e contra as fontes da versão publicada do formulário. Se o relatório do
    mapeamento novo tem pendências, uma integração ativa é desativada: ela não pode seguir enviando
    com elas.

    Levanta `NotPublished`, `integration_config.Conflict` (a integração mudou desde que a pessoa a
    viu, ou já não existe: a versão esperada é conferida **antes** de ler a tabela), `ReadFailed` (a
    tabela não foi lida: nada é gravado) e `InvalidMapping` (de `app.integrations.mapping`; `problems`
    diz o que corrigir)."""
    spec = integration_config.form_spec(deps, form_slug)
    if spec is None:
        raise NotPublished
    with deps.sessions() as db:
        before = integration_config.load(db, form_slug)
    if before is None or before.version != expected_version:
        # Sem integração (removida, ou nunca conectada) ou com outra versão: conflito, como em
        # `connect`, antes de ler a tabela.
        raise integration_config.Conflict(before)
    try:
        live = read_fields(deps, form_slug)
    except IntegrationMissing:  # removida entre o `load` e a leitura
        raise integration_config.Conflict(None) from None
    sources = {field_id: source.strip() for field_id, source in chosen.items() if source.strip()}
    entries = [
        integration_mapping.Entry(item.id, item.name, item.type, sources[item.id])
        for item in live
        if item.id in sources
    ]
    integration_mapping.check_entries(entries, live, integration_mapping.catalog(spec))
    report = integration_mapping.build_report(entries, live, spec)
    with deps.sessions.begin() as db:
        saved = integration_config.save_mapping(
            db,
            form_slug=form_slug,
            entries=entries,
            actor=actor,
            expected_version=expected_version,
            deactivate=not report.ok,
        )
    logger.info(
        "admin: mapeamento da integração formulario=%s campos=%s pendencias=%s por=%s",
        form_slug,
        len(entries),
        len(report.problems) + report.unmatched,
        actor,
    )
    return MappingSaved(integration=saved, report=report, was_active=before.active)


def activate(
    deps: Deps, form_slug: str, *, expected_version: int, actor: str
) -> integration_config.StoredIntegration:
    """Liga a integração, se o mapeamento guardado, conferido contra a tabela **como ela está agora** e
    contra as perguntas da versão publicada, não tem pendência (`build_report` limpo). Os pedidos que
    ficarem prontos daqui em diante viram uma linha na tabela; os que já estavam prontos não são
    sincronizados.

    A versão esperada se compara **antes** de ler a tabela, como em `connect` e `save_mapping`.

    Levanta `NotPublished`, `IntegrationMissing`, `integration_config.Conflict` (a integração mudou
    desde que a pessoa a viu, ou mudou enquanto o Baserow era lido), `ReadFailed` (a tabela não foi
    lida: `TokenUnreadable`, o Baserow nem é chamado, ou `TableNotRead`) e `ReportPending` (o
    relatório tem pendências, ou nada está mapeado). Em todas, nada muda."""
    spec = integration_config.form_spec(deps, form_slug)
    if spec is None:
        raise NotPublished
    with deps.sessions() as db:
        stored = integration_config.load(db, form_slug)
    if stored is None:
        raise IntegrationMissing
    if stored.version != expected_version:
        raise integration_config.Conflict(stored)
    try:
        live = read_fields(deps, form_slug)
    except IntegrationMissing:  # removida entre o `load` e a leitura
        raise integration_config.Conflict(None) from None
    report = integration_mapping.build_report(list(stored.entries), live, spec)
    if not report.ok:
        raise ReportPending(report, nothing_mapped=not stored.entries)
    with deps.sessions.begin() as db:
        activated = integration_config.set_active(
            db, form_slug=form_slug, active=True, actor=actor, expected_version=expected_version
        )
    logger.info("admin: integração ativada formulario=%s por=%s", form_slug, actor)
    return activated


def deactivate(
    deps: Deps, form_slug: str, *, expected_version: int, actor: str
) -> integration_config.StoredIntegration:
    """Desliga a integração: os pedidos novos deixam de virar linha, e as sincronizações que ainda
    estavam repetindo esperam a reativação. Não fala com o Baserow, e funciona com o token que não abre.

    Levanta `integration_config.Conflict` (a integração mudou desde que a pessoa a viu, ou já não
    existe)."""
    with deps.sessions.begin() as db:
        deactivated = integration_config.set_active(
            db, form_slug=form_slug, active=False, actor=actor, expected_version=expected_version
        )
    logger.info("admin: integração desativada formulario=%s por=%s", form_slug, actor)
    return deactivated


def remove(deps: Deps, form_slug: str, *, expected_version: int, actor: str) -> Removed:
    """Remove a integração: apaga o token e o estado das sincronizações. As linhas que já estão na
    tabela do Baserow ficam: o app não apaga nada lá. Devolve a integração como estava e as
    sincronizações que saíram com ela, por estado.

    Levanta `integration_config.Conflict` (a integração mudou desde que a pessoa a viu, ou já não
    existe): nada é removido."""
    with deps.sessions.begin() as db:
        before = integration_config.load(db, form_slug)
        if before is None:
            raise integration_config.Conflict(None)
        counts = integration_syncs.counts(db, before.id)
        integration_config.remove(
            db, form_slug=form_slug, actor=actor, expected_version=expected_version
        )
    logger.info(
        "admin: integração removida formulario=%s sincronizacoes=%s por=%s",
        form_slug,
        counts.pending + counts.succeeded + counts.failed,
        actor,
    )
    return Removed(integration=before, sync_counts=counts)


def send_test(deps: Deps, form_slug: str, *, actor: str) -> integration_push.SampleResult:
    """Manda para a tabela a linha de teste (a lead de exemplo, no domínio `exemplo`), pelo mesmo caminho
    de um pedido de verdade, e devolve o que aconteceu. A linha fica na tabela; testar de novo atualiza
    a mesma. Não passa pelo Hatchet nem toca dado de lead, e o resultado fica na auditoria. Um teste que
    o Baserow recusou **não é erro**: é um resultado (`ok` falso, com o motivo).

    Levanta `IntegrationMissing` e `TokenUnreadable` (o token guardado não abre: nada é mandado, e o
    Baserow nem é chamado)."""
    try:
        result = integration_push.send_sample(deps, form_slug, actor=actor)
    except destinations.UnreadableSecret:
        raise TokenUnreadable from None
    if result is None:
        raise IntegrationMissing
    logger.info(
        "admin: envio de teste da integração formulario=%s ok=%s resultado=%s por=%s",
        form_slug,
        result.ok,
        result.outcome,
        actor,
    )
    return result


def resend_failed(deps: Deps, form_slug: str, *, actor: str) -> FailedResent:
    """Põe de volta na fila, de uma vez, as sincronizações que falharam (até
    `launching.RESEND_FAILED_LIMIT`, as mais antigas primeiro): o que se faz depois de corrigir o token,
    a tabela ou o mapeamento. A ação **não dispara**: quem chama dispara os `sync_ids`
    (`launching.launch_syncs_later` ou `launching.launch_syncs`), e o reconciliador dispara o que ficar
    sem run id. Sem nenhuma falhada, nada é gravado nem auditado.

    Levanta `IntegrationMissing`."""
    with deps.sessions.begin() as db:
        stored = integration_config.load(db, form_slug)
        if stored is None:
            raise IntegrationMissing
        found = integration_syncs.resend_failed(
            db, stored.id, actor=actor, limit=launching.RESEND_FAILED_LIMIT
        )
        remaining = integration_syncs.counts(db, stored.id).failed
    if found:
        logger.info(
            "admin: reenvio das sincronizações falhadas formulario=%s total=%s por=%s",
            form_slug,
            len(found),
            actor,
        )
    return FailedResent(sync_ids=found, still_failed=remaining)
