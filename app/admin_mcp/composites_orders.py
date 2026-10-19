"""As compostas dos pedidos: `attention_overview` e `diagnose_order` (só leem).

Ver `composites.py` (o quadro das cinco) e `compose.py` (as regras).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.admin_mcp.catalog import ToolSpec
from app.admin_mcp.compose import (
    Composite,
    Steps,
    body,
    borrowed,
    build,
    first_line,
    flag,
    integer,
    lines_of,
    listed,
    plain,
    succeeded,
)
from app.admin_mcp.results import ToolOutput

# Integrações lidas por chamada. Cada leitura chama o Baserow e custa 5 fichas do limite de uso, e o
# balde da chave tem 30 de rajada (`app/admin/api/limits.py`): a visão geral gasta 4 fichas nas outras
# leituras, então 4 integrações (24 fichas) cabem num balde cheio, e 5 deixariam uma ficha.
MAX_INTEGRATIONS = 4
MAX_SLUGS = 5  # o que `integration_slugs` aceita (5 leituras e as 4 outras cabem em 29 fichas)
MAX_PAGES = 5  # páginas de formulários que a visão geral percorre (200 por página)
MAX_EVENTS = 10  # acontecimentos da linha do tempo da tentativa atual em `diagnose_order`


# =================================================================================================
# attention_overview
# =================================================================================================


def attention_overview(tools: Mapping[str, ToolSpec]) -> Composite | None:
    return build(
        tools,
        name="attention_overview",
        summary="Ver o que pede atenção agora",
        details=(
            "Numa chamada, o que a operadora confere de manhã: os pedidos que falharam, as entregas ao "
            "webhook que falharam (a tentativa atual de qualquer pedido, não só dos mais recentes), "
            "os formulários sem destino (os planos deles "
            "não são entregues), os segredos que não abrem (o do destino e o token do Baserow), as "
            "sincronizações com a planilha que falharam e os rascunhos ainda não publicados. Cada seção "
            "é lida à parte: se uma não pôde ser lida, o texto diz qual e traz as outras. Ler a "
            "integração chama o Baserow e custa 5 fichas do limite de uso (o balde da chave tem 30): "
            f"por isso lê só {MAX_INTEGRATIONS} formulários por chamada, e diz quais ficaram de fora "
            "(`integration_slugs` escolhe quais ler). Não traz nome, e-mail nem resposta de lead."
        ),
        calls=("list_jobs", "list_destinations", "list_forms", "get_form_integration"),
        properties={
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
                "description": "Quantos itens listar em cada seção (de 1 a 50; padrão 10). O resto "
                "vem só como contagem.",
            },
            "integrations": {
                "type": "boolean",
                "default": True,
                "description": "`false` não lê a integração com o Baserow dos formulários (pula as "
                "sincronizações que falharam e o token que não abre), para poupar fichas do limite de "
                "uso.",
            },
            "integration_slugs": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_SLUGS,
                "description": "Os formulários (`slug`) cuja integração com o Baserow ler, no lugar "
                f"dos {MAX_INTEGRATIONS} primeiros no ar (até {MAX_SLUGS}; cada leitura custa 5 fichas "
                "do limite de uso).",
            },
        },
        run=_attention_overview,
    )


class _Overview:
    """O que a visão geral junta: as seções com algo a olhar, as que estão em ordem e as que não deu
    para ler."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.findings: list[list[str]] = []
        self.in_order: list[str] = []
        self.unread: list[str] = []
        self.skipped: list[str] = []
        self.reads = 0

    def found(self, title: str, items: Sequence[str], advice: str | None = None) -> None:
        self.findings.append([title, *listed(items, self.limit), *([advice] if advice else [])])

    def failed(self, what: str, output: ToolOutput) -> None:
        self.unread.append(f"{what}: {first_line(output)}")


async def _attention_overview(steps: Steps, arguments: dict[str, Any]) -> ToolOutput:
    overview = _Overview(integer(arguments, "limit", 10, 1, 50))
    integrations = flag(arguments, "integrations", True)
    await _failed_orders(steps, overview)
    await _failed_deliveries(steps, overview)
    unreadable = await _destinations(steps, overview)
    forms = await _forms(steps, overview)
    tokens: list[str] = []
    if not integrations:
        overview.skipped.append("a integração com o Baserow (`integrations: false`)")
    elif forms is not None:
        tokens = await _integrations(steps, overview, forms, arguments.get("integration_slugs"))
    secrets = [*(unreadable or []), *tokens]
    if secrets:
        overview.found(
            f"Segredos que não abrem: {len(secrets)}",
            secrets,
            "Gere um segredo novo (`rotate_destination_secret`) ou mande o token de novo "
            "(`connect_baserow_integration`); as entregas e as sincronizações falham até lá.",
        )
    elif unreadable is not None:
        overview.in_order.append("os segredos dos destinos abrem")
    if overview.reads == 0:
        header = "A visão geral falhou: não consegui ler nada."
    elif overview.findings:
        header = f"PEDE ATENÇÃO: {len(overview.findings)} ponto(s)."
    elif overview.unread:
        header = "Nada pede atenção no que consegui ler."  # e o que não li está logo abaixo
    else:
        header = "Nada pede atenção agora."
    blocks: list[Sequence[str]] = [[header], *overview.findings]
    if overview.in_order:
        blocks.append(["Em ordem: " + "; ".join(overview.in_order) + "."])
    if overview.skipped:
        blocks.append(["Não conferi, a seu pedido: " + "; ".join(overview.skipped) + "."])
    if overview.unread:
        blocks.append(
            [
                "NÃO CONSEGUI LER (a visão geral está incompleta):",
                *listed(overview.unread, len(overview.unread)),
            ]
        )
    return ToolOutput(lines_of(*blocks), is_error=overview.reads == 0)


async def _failed_orders(steps: Steps, overview: _Overview) -> None:
    listing = await steps.call(
        "list_jobs", {"status": "failed", "limit": overview.limit, "include_total": True}
    )
    if not succeeded(listing):
        return overview.failed("pedidos que falharam", listing)
    overview.reads += 1
    page = body(listing)
    jobs = page.get("items") or []
    if not jobs:
        return overview.in_order.append("nenhum pedido falhou")
    total = page.get("total")
    overview.found(
        f"Pedidos que falharam: {total if isinstance(total, int) else len(jobs)}",
        [_failed_job(job) for job in jobs],
        "Veja um deles com `diagnose_order`.",
    )


async def _failed_deliveries(steps: Steps, overview: _Overview) -> None:
    """As entregas que falharam, de qualquer pedido: é a API que filtra (`delivery=failed`, na tentativa
    atual do pedido, a que a lista mostra), em vez de baixar os pedidos mais recentes e procurar."""
    listing = await steps.call(
        "list_jobs", {"delivery": "failed", "limit": overview.limit, "include_total": True}
    )
    if not succeeded(listing):
        return overview.failed("entregas ao webhook que falharam", listing)
    overview.reads += 1
    page = body(listing)
    jobs = page.get("items") or []
    if not jobs:
        return overview.in_order.append("nenhuma entrega ao webhook falhou")
    total = page.get("total")
    overview.found(
        f"Entregas ao webhook que falharam: {total if isinstance(total, int) else len(jobs)} pedido(s)",
        [_delivery_line(job) for job in jobs],
        "Veja um deles com `diagnose_order`; confira o destino em `list_destinations`.",
    )


async def _destinations(steps: Steps, overview: _Overview) -> list[str] | None:
    """Põe nas seções os formulários sem destino; devolve os escopos cujo segredo não abre, ou `None`
    se a lista não foi lida."""
    listing = await steps.call("list_destinations", {"limit": 1})
    if not succeeded(listing):
        overview.failed("destinos", listing)
        return None
    overview.reads += 1
    found = body(listing)  # `unserved` e `unreadable` vêm inteiras, qualquer que seja a página
    unserved = [plain(slug) for slug in found.get("unserved") or []]
    if unserved:
        overview.found(
            f"Formulários sem destino (os planos deles não são entregues): {len(unserved)}",
            unserved,
            "Cadastre um destino padrão ou do formulário com `save_destination` (chave `total`).",
        )
    else:
        overview.in_order.append("todo formulário tem destino")
    return [
        f"destino «{plain(scope)}»: o segredo não abre com a chave de hoje"
        for scope in found.get("unreadable") or []
    ]


async def _forms(steps: Steps, overview: _Overview) -> list[dict[str, Any]] | None:
    """Lê os formulários (até `MAX_PAGES` páginas) e põe nas seções os rascunhos pendentes, as versões
    que não compilam e os avisos dos dados. Devolve a lista, ou `None` se a primeira página falhou."""
    forms: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        asked: dict[str, Any] = {"limit": 200}
        if cursor:
            asked["cursor"] = cursor
        listing = await steps.call("list_forms", asked)
        if not succeeded(listing):
            overview.failed("formulários", listing)
            if not forms:
                return None
            break
        page = body(listing)
        forms += [form for form in page.get("items") or [] if isinstance(form, dict)]
        cursor = page.get("next_cursor") or None
        if not cursor:
            break
    overview.reads += 1
    if cursor:
        overview.unread.append(f"formulários: li os primeiros {len(forms)}, há mais")
    pending = [form for form in forms if form.get("draft_pending")]
    if pending:
        overview.found(
            f"Rascunhos ainda não publicados: {len(pending)}",
            [_pending_draft(form) for form in pending],
            "Compare com `compare_form_versions` e publique com `publish_form_with_review`.",
        )
    else:
        overview.in_order.append("nenhum rascunho pendente")
    broken = [form for form in forms if form.get("version_compiles") is False]
    if broken:
        overview.found(
            f"Formulários cuja versão publicada não compila neste deploy (fora do ar): {len(broken)}",
            [plain(form.get("slug")) for form in broken],
            "Publique uma versão nova que compile.",
        )
    notices = [form for form in forms if form.get("data_notices")]
    if notices:
        overview.found(
            f"Formulários com o que conferir nos dados: {len(notices)}",
            [_notices(form) for form in notices],
        )
    return forms


async def _integrations(
    steps: Steps, overview: _Overview, forms: Sequence[Mapping[str, Any]], chosen: Any
) -> list[str]:
    """Lê a integração dos formulários no ar (`MAX_INTEGRATIONS` por chamada, ou os `chosen`) e põe nas
    seções as sincronizações que falharam e as pendências; devolve os tokens que não abrem."""
    live = [
        str(form["slug"])
        for form in forms
        if form.get("active") and form.get("published_version") is not None and form.get("slug")
    ]
    if chosen is None:
        slugs, left_out = live[:MAX_INTEGRATIONS], live[MAX_INTEGRATIONS:]
    else:
        slugs = [str(item) for item in chosen][:MAX_SLUGS]
        left_out = [slug for slug in live if slug not in slugs]
    failed_syncs: list[str] = []
    to_check: list[str] = []
    tokens: list[str] = []
    checked = 0
    for name in slugs:
        slug = plain(name)
        reading = await steps.call("get_form_integration", {"slug": name})
        if not succeeded(reading):
            overview.failed(f"integração de «{slug}»", reading)
            if reading.status == 429:  # o limite de uso acabou: perguntar de novo só piora
                break
            continue
        checked += 1
        integration = body(reading)
        if not integration.get("connected"):
            continue
        connection = integration.get("connection") or {}
        counts = integration.get("sync_counts") or {}
        if counts.get("failed"):
            failed_syncs.append(_failed_syncs(slug, counts, integration.get("failed_syncs") or []))
        if connection.get("token_unreadable"):
            tokens.append(f"token do Baserow de «{slug}»: não abre com a chave de hoje")
        elif integration.get("fields_error"):
            to_check.append(
                f"«{slug}»: não li a tabela do Baserow ({plain(integration['fields_error'])})"
            )
        report = integration.get("report")
        if connection.get("active") and isinstance(report, dict) and report.get("ok") is False:
            problems = "; ".join(plain(p, 120) for p in (report.get("problems") or [])[:3])
            to_check.append(
                f"«{slug}»: integração ativa com pendências no mapeamento"
                + (f" ({problems})" if problems else "")
            )
    if left_out:
        overview.unread.append(
            "integração de "
            + ", ".join(f"«{plain(slug, 60)}»" for slug in left_out)
            + f" (leio {MAX_INTEGRATIONS} por chamada, por causa do limite de uso: passe "
            "`integration_slugs` ou use `get_form_integration`)"
        )
    if checked:
        overview.reads += 1
    if failed_syncs:
        overview.found(
            f"Sincronizações com a planilha que falharam: {len(failed_syncs)} formulário(s)",
            failed_syncs,
            "Corrija o token, a tabela ou o mapeamento e reenvie com `resend_job_sync` "
            "(chave `total`); `diagnose_order` mostra o pedido.",
        )
    elif checked:
        overview.in_order.append("nenhuma sincronização com a planilha falhou")
    if to_check:
        overview.found("Integrações com o Baserow a conferir:", to_check)
    return tokens


def _failed_job(job: Mapping[str, Any]) -> str:
    return (
        f"`{plain(job.get('public_id'))}` · {plain(job.get('form_slug'))} "
        f"(v{plain(job.get('form_version'))}) · falha: {plain(job.get('failure_code') or 'sem código')}"
        f" · tentativa {plain(job.get('attempt'))} · {plain(job.get('created_at'))}"
    )


def _delivery_line(job: Mapping[str, Any]) -> str:
    failed = [
        d
        for d in job.get("deliveries") or []
        if isinstance(d, dict) and d.get("status") == "failed"
    ]
    return (
        f"`{plain(job.get('public_id'))}` · {plain(job.get('form_slug'))} · "
        f"{len(failed)} entrega(s) com falha · {plain(job.get('created_at'))}"
    )


def _pending_draft(form: Mapping[str, Any]) -> str:
    version = form.get("published_version")
    where = f"publicada: v{version}" if version is not None else "ainda sem versão publicada"
    return f"{plain(form.get('slug'))} ({where})"


def _notices(form: Mapping[str, Any]) -> str:
    found = "; ".join(plain(n, 100) for n in form.get("data_notices") or [])
    return f"{plain(form.get('slug'))}: {found}"


def _failed_syncs(slug: str, counts: Mapping[str, Any], failed: Sequence[Any]) -> str:
    shown = ", ".join(
        f"`{plain(item.get('job_public_id'))}` ({plain(item.get('failure_code') or 'sem código')})"
        for item in failed[:5]
        if isinstance(item, dict)
    )
    return f"«{slug}»: {counts.get('failed')} falharam, {counts.get('pending', 0)} pendentes" + (
        f": {shown}" if shown else ""
    )


# =================================================================================================
# diagnose_order
# =================================================================================================

FAILURE_HINTS = {
    "llm_unavailable": "o provedor da IA não respondeu; se já voltou, reprocessar costuma resolver.",
    "llm_rejected": "a IA recusou o pedido; reprocessar sem mudar nada tende a falhar de novo: "
    "confira o prompt e as instruções do formulário (`get_form_draft`).",
    "plan_invalid": "a IA devolveu um plano fora do modelo do plano; confira o modelo e o prompt "
    "(`get_form_draft`) antes de reprocessar.",
    "render_failed": "o PDF não montou; confira o template do PDF com "
    "`render_form_draft_pdf_preview` antes de reprocessar.",
    "storage_failed": "o armazenamento do PDF falhou; confira o S3 e reprocesse.",
    "stuck": "o pedido parou no meio e foi marcado como travado; reprocessar o põe na fila de novo.",
    "internal": "erro interno do app; veja o log e, se foi passageiro, reprocesse.",
}


def diagnose_order(tools: Mapping[str, ToolSpec]) -> Composite | None:
    if "get_job" not in tools:
        return None
    properties = borrowed(tools, "get_job", "public_id")["properties"]
    if "include_lead_data" in tools["get_job"].locations:
        # Só existe com `GERADOR_MCP_LEAD_DATA=1` e uma chave `total`: o catálogo o tira do `get_job`
        # sem a opção, e a composta o acompanha.
        properties["include_lead_data"] = {
            "type": "boolean",
            "default": False,
            "description": "`true` lê também os textos livres dos erros (o detalhe da falha e o último "
            "erro de cada entrega e sincronização, que podem citar a lead). Nome, e-mail e respostas "
            "da lead nunca saem no texto desta ferramenta. Padrão: `false`.",
        }
    return build(
        tools,
        name="diagnose_order",
        summary="Diagnosticar um pedido e dizer o que fazer",
        details=(
            "Lê o pedido (`get_job`) e diz, em português: em que pé está, por que falhou, a situação de "
            "cada entrega ao webhook e de cada sincronização com a planilha, a linha do tempo da "
            "tentativa atual e o que fazer. **O conserto ela só sugere**, com os argumentos prontos "
            "(`reprocess_job`, `resend_job_delivery`, `resend_job_sync`): não os chama, e cada um é uma "
            "ação marcada que pede a confirmação da pessoa quando você a chamar. Não traz nome, e-mail "
            "nem resposta de lead."
        ),
        calls=("get_job",),
        properties=properties,
        required=("public_id",),
        run=_diagnose_order,
    )


async def _diagnose_order(steps: Steps, arguments: dict[str, Any]) -> ToolOutput:
    asked: dict[str, Any] = {"public_id": arguments["public_id"]}
    if arguments.get("include_lead_data") is True:
        asked["include_lead_data"] = True
    reading = await steps.call("get_job", asked)
    if not succeeded(reading):
        return ToolOutput(reading.text, is_error=True, status=reading.status)
    job = body(reading)
    attempt = job.get("attempt")
    everything = [*(job.get("deliveries") or []), *(job.get("syncs") or [])]
    deliveries = [d for d in job.get("deliveries") or [] if _is_of(d, attempt)]
    syncs = [s for s in job.get("syncs") or [] if _is_of(s, attempt)]
    older = [x for x in everything if isinstance(x, dict) and not _is_of(x, attempt)]
    older_failed = [x for x in older if x.get("status") == "failed"]
    pdfs = [p for p in job.get("pdfs") or [] if isinstance(p, dict)]
    sync_block = [
        "Sincronizações com a planilha (tentativa atual):",
        *listed([_sync(s) for s in syncs] or ["nenhuma"]),
    ]
    if older_failed:
        sync_block.append(
            f"({len(older_failed)} entrega(s) ou sincronização(ões) de tentativas anteriores falharam "
            "e ficam só no histórico; `get_job` as lista.)"
        )
    return ToolOutput(
        lines_of(
            _order_header(job),
            [
                "Entregas ao webhook (tentativa atual):",
                *listed([_delivery(d) for d in deliveries] or ["nenhuma"]),
            ],
            sync_block,
            [
                "PDFs: "
                + (
                    "; ".join(
                        f"tentativa {p.get('version')}: {p.get('byte_size')} bytes"
                        + (" (apagado pela retenção)" if p.get("purged_at") else "")
                        for p in pdfs
                    )
                    or "nenhum"
                )
            ],
            _events(job, attempt),
            _notes(job) if arguments.get("include_lead_data") is True else [],
            ["O que fazer:", *listed(_what_to_do(steps, job, deliveries, syncs))],
        )
    )


def _is_of(item: Any, attempt: Any) -> bool:
    return isinstance(item, dict) and item.get("attempt") == attempt


def _order_header(job: Mapping[str, Any]) -> list[str]:
    done = f", concluído em {plain(job['completed_at'])}" if job.get("completed_at") else ""
    lines = [
        (
            f"Pedido `{plain(job.get('public_id'))}` · formulário {plain(job.get('form_slug'))} "
            f"(v{plain(job.get('form_version'))})"
        ),
        (
            f"Situação: {plain(job.get('status'))} (etapa {plain(job.get('stage'))}), tentativa "
            f"{plain(job.get('attempt'))}, chegou em {plain(job.get('created_at'))}{done}."
        ),
    ]
    if job.get("link_expires_at"):
        lines.append(
            f"O link de resultado da lead {'JÁ VENCEU' if job.get('link_expired') else 'ainda vale'} "
            f"(até {plain(job['link_expires_at'])})."
        )
    if job.get("failure_code"):
        code = plain(job["failure_code"])
        lines.append(
            f"Falha: `{code}`" + (f" — {FAILURE_HINTS[code]}" if code in FAILURE_HINTS else ".")
        )
    if job.get("run_url"):
        lines.append(f"Execução no Hatchet: {plain(job['run_url'], 300)}")
    return lines


def _delivery(delivery: Mapping[str, Any]) -> str:
    scope = f", destino {plain(delivery['scope'])}" if delivery.get("scope") else ""
    host = f" em {plain(delivery['host'])}" if delivery.get("host") else ""
    return (
        f"`{plain(delivery.get('id'))}`: {plain(delivery.get('status'))}, "
        f"{plain(delivery.get('attempts'))} tentativa(s) de envio{host}{scope}"
    )


def _sync(sync: Mapping[str, Any]) -> str:
    outcome = f", {plain(sync['outcome'])}" if sync.get("outcome") else ""
    return (
        f"`{plain(sync.get('id'))}`: {plain(sync.get('status'))}{outcome}, "
        f"{plain(sync.get('attempts'))} tentativa(s) de envio, tabela {plain(sync.get('table_id'))} "
        f"em {plain(sync.get('host'))}"
    )


def _events(job: Mapping[str, Any], attempt: Any) -> list[str]:
    for item in job.get("timeline") or []:
        if not _is_of(item, attempt):
            continue
        events = [e for e in item.get("events") or [] if isinstance(e, dict)]
        shown = [
            f"{plain(e.get('at') or '?')} · {plain(e.get('type'))}"
            + "".join(
                f" ({key} {plain(e[key])})"
                for key in ("stage", "failure_code", "actor")
                if e.get(key)
            )
            for e in events[-MAX_EVENTS:]
        ]
        title = f"Linha do tempo da tentativa {plain(attempt)}" + (
            f" (desfecho: {plain(item['outcome'])})" if item.get("outcome") else ""
        )
        more = (
            [f"(só os últimos {MAX_EVENTS} de {len(events)})"] if len(events) > MAX_EVENTS else []
        )
        return [f"{title}:", *listed(shown), *more]
    return []


def _notes(job: Mapping[str, Any]) -> list[str]:
    lead = job.get("lead_data")
    found = lead.get("notes") if isinstance(lead, dict) else None
    if not found:
        return ["Textos livres dos erros (dado da lead, a seu pedido): nenhum."]
    return [
        (
            "Textos livres dos erros (dado da lead, a seu pedido; nome, e-mail e respostas não são "
            "repetidos aqui):"
        ),
        *listed(
            [
                f"tentativa {plain(n.get('attempt'))} · {plain(n.get('kind'))}"
                + (f" · `{plain(n['reference'])}`" if n.get("reference") else "")
                + f": {plain(n.get('text'), 300)}"
                for n in found
                if isinstance(n, dict)
            ]
        ),
    ]


def _what_to_do(
    steps: Steps, job: Mapping[str, Any], deliveries: Sequence[Any], syncs: Sequence[Any]
) -> list[str]:
    def tool(name: str, how: str) -> str:
        if steps.has(name):
            return f"{how} (`{name}`; ação marcada: pede a confirmação da pessoa)"
        return f"{how} (`{name}` exige uma chave `total`: peça a quem tem uma)"

    public_id = plain(job.get("public_id"))
    status = job.get("status")
    todo: list[str] = []
    if status in ("queued", "running"):
        todo.append(
            f"O pedido está {'na fila' if status == 'queued' else 'sendo gerado'}: espere. "
            "Reprocessar agora é recusado (409); se ficar parado, o sistema o marca como travado."
        )
    elif status == "failed":
        todo.append(
            tool(
                "reprocess_job",
                f"Reprocessar: `public_id: {public_id}`, `expected_attempt: {plain(job.get('attempt'))}` "
                "e um `reason` de 10 a 300 caracteres que não cite a lead; chama a IA (paga) e o "
                "webhook de novo",
            )
        )
    elif status == "completed":
        todo.append("O pedido está concluído.")
    quiet = True
    for delivery in deliveries:
        if delivery.get("status") == "failed":
            quiet = False
            todo.append(
                tool(
                    "resend_job_delivery",
                    f"Reenviar a entrega `{plain(delivery.get('id'))}`: `public_id: {public_id}`, "
                    f"`delivery_id: {plain(delivery.get('id'))}`; antes, confira o destino com "
                    "`list_destinations`, porque ela vai para o que vale agora",
                )
            )
        elif delivery.get("status") == "pending":
            quiet = False
            todo.append(
                f"A entrega `{plain(delivery.get('id'))}` ainda não saiu: o reconciliador a dispara em "
                "até poucos minutos; não reenvie."
            )
    for sync in syncs:
        if sync.get("status") == "failed":
            quiet = False
            todo.append(
                tool(
                    "resend_job_sync",
                    f"Reenviar a sincronização `{plain(sync.get('id'))}`: `public_id: {public_id}`, "
                    f"`sync_id: {plain(sync.get('id'))}`; antes, corrija o token, a tabela ou o "
                    "mapeamento (`get_form_integration` mostra o relatório)",
                )
            )
        elif sync.get("status") == "pending":
            quiet = False
            todo.append(f"A sincronização `{plain(sync.get('id'))}` está pendente: espere.")
    if quiet and status == "completed":
        todo.append(
            "Nada a fazer: as entregas e as sincronizações da tentativa atual estão em ordem."
        )
    return todo
