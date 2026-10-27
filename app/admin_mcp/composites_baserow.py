"""A composta da planilha: `connect_baserow_integration`.

Ver `composites.py` (o quadro das cinco) e `compose.py` (as regras).

**O mapeamento tem a confirmação dele.** `save_form_integration_mapping` **não é marcada na API**: ela
troca o mapeamento sem `confirm`, e numa integração **ativa** isso muda o que a planilha do cliente
passa a receber (e um mapeamento com pendências a desativa). A ferramenta gerada segue assim, porque o
contrato é da API; a composta, não:

- quando o mapeamento a guardar vem em `sources`, ele (e o que ele troca) está no que a pessoa vê ao
  confirmar a conexão e entra na impressão digital dessa confirmação (`bind`): repetir a chamada com
  outro `sources` é outra ação, e pergunta de novo;
- quando a integração está **ativa**, guardar o mapeamento é um passo com a confirmação própria
  (`Steps.gate`), que mostra o que há hoje e o que passa a haver; para não deixar a conexão feita e a
  troca por confirmar (a repetição da chamada refaria a conexão), numa integração ativa e inalterada a
  troca vem **antes** da conexão, e é pulada quando o mapeamento guardado já é o pedido.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from app.admin_mcp.catalog import ToolSpec
from app.admin_mcp.compose import (
    Composite,
    Progress,
    Steps,
    body,
    borrowed,
    build,
    confirmations,
    flag,
    lines_of,
    listed,
    plain,
    refused,
    succeeded,
    waiting,
)
from app.admin_mcp.results import ToolOutput

MAX_HOST = (
    253  # o maior nome de servidor que existe (DNS): acima disso o endereço não é de servidor
)
MAX_FIELDS_SHOWN = 100  # campos mapeados que a pergunta lista (uma tabela com mais corta o resto)

# =================================================================================================
# connect_baserow_integration
# =================================================================================================


def connect_baserow_integration(tools: Mapping[str, ToolSpec]) -> Composite | None:
    calls = (
        "get_form_integration",
        "connect_form_integration",
        "save_form_integration_mapping",
        "activate_form_integration",
    )
    if not all(call in tools for call in calls):
        return None
    if "purpose" not in tools["connect_form_integration"].input_schema.get("properties", {}):
        return None
    properties = borrowed(
        tools,
        "connect_form_integration",
        "slug",
        "base_url",
        "table_id",
        "token",
        "confirm",
        "purpose",
    )["properties"]
    properties["sources"] = {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": 'O mapeamento a guardar, `{"<id do campo no Baserow>": "<fonte>"}` (as fontes '
        "de cada campo estão em `fields` de `get_form_integration`; a finalidade publicada seleciona o perfil PDF ou captura). Sem ele, a ferramenta guarda a "
        "sugestão pelo nome dos campos se nada está mapeado, e mantém o mapeamento que existe. Com "
        "ele, troca o mapeamento inteiro: a pessoa vê o que há hoje e o que passa a haver, e numa "
        "integração ativa a troca pede a confirmação própria dela.",
    }
    properties["activate"] = {
        "type": "boolean",
        "default": False,
        "description": "`true` é o **segundo passo**: ativa a integração já conectada e mapeada, depois "
        "de mostrar o relatório e de a pessoa confirmar. Vai sozinho (só com `slug`, e `confirm` se "
        "o servidor pedir): não se conecta e se ativa na mesma chamada. `false` (o padrão): conecta, "
        "mapeia e mostra o relatório, sem ativar.",
    }
    properties["expected_version"] = {
        "type": "string",
        "description": "A `version` da integração que você viu (`get_form_integration`; `0` se não "
        "existe). Se mudou, a API responde `409 version_conflict` e nada é gravado. Sem ela, vale a de "
        "agora.",
    }
    return build(
        tools,
        name="connect_baserow_integration",
        summary="Conectar um formulário a uma tabela do Baserow e mapear os campos",
        details=(
            "Em dois passos. **Primeiro** (`activate` ausente): lê a integração (`get_form_integration`), "
            "conecta o formulário à tabela (`connect_form_integration`, ação marcada que **sobrescreve a "
            "conexão**: mudar o endereço ou a tabela apaga o mapeamento e desativa a integração; o "
            "servidor pergunta à pessoa, mostrando o servidor por inteiro e o mapeamento que você mandou "
            "em `sources`, se mandou), lê os campos da tabela, guarda o mapeamento (o que você mandou em "
            "`sources`, ou a sugestão pelo nome dos campos quando nada está mapeado; uma integração "
            "recém-conectada nasce desativada) e mostra o relatório contra a tabela. Guardar o "
            "mapeamento (`save_form_integration_mapping`) a API não marca, mas numa integração **ativa** "
            "a troca pede a confirmação própria da pessoa, com o mapeamento de hoje e o novo. **Depois**, "
            "com `activate: true` e só o `slug`: relê o relatório e, se está limpo, ativa a integração "
            "(`activate_form_integration`, ação marcada: cada pedido concluído passa a virar uma linha "
            "na planilha do cliente; o servidor pergunta à pessoa, listando cada campo mapeado e o "
            "relatório). Nunca ativa sem a confirmação dela, e nunca junto com a conexão. Chama o "
            "Baserow (5 fichas do limite de uso por leitura). O token entra e não volta: não aparece em "
            "nenhum texto desta ferramenta."
        ),
        calls=calls,
        properties=properties,
        required=("slug",),
        run=_connect_baserow_integration,
    )


# --- o endereço e o mapeamento, como a pessoa os vê --------------------------------------------------


def server_of(base_url: Any) -> tuple[str, str] | None:
    """O servidor de `base_url` **por inteiro** (`esquema://servidor[:porta]`) e o caminho, ou `None` se
    o endereço não tem servidor que se possa mostrar: não se lê como URL, não tem servidor, o servidor
    passa de 253 caracteres ou tem caractere de controle ou espaço. O que a pessoa confirma é para onde o
    token vai, então ele não pode sair cortado nem mudar de forma ao ser limpo."""
    try:
        parts = urlsplit(str(base_url))
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or len(host) > MAX_HOST or not host.isprintable():
        return None
    if any(ch.isspace() for ch in host):
        return None
    shown = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{shown}{f':{port}' if port is not None else ''}", parts.path


def _shown_server(base_url: Any) -> str:
    """O servidor para o texto: por inteiro, e o caminho (que não decide para onde o token vai) cortado."""
    found = server_of(base_url)
    if found is None:
        return plain(base_url, 120)
    server, path = found
    return server + (f" (caminho {plain(path, 80)})" if path.strip("/") else "")


def _sources_of(value: Any) -> dict[str, str] | None:
    return None if value is None else {str(key): str(source) for key, source in dict(value).items()}


def _purpose_state(state: Mapping[str, Any]) -> Mapping[str, Any]:
    """A composta opera o perfil publicado sem confundir os mapeamentos históricos."""
    if state.get("profile") == "capture":
        return {**state, "mapping": state.get("capture_mapping") or []}
    return state


def _stored_sources(state: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(entry.get("field_id")): str(entry.get("source"))
        for entry in state.get("mapping") or []
        if isinstance(entry, dict)
    }


def _names(state: Mapping[str, Any]) -> dict[str, str]:
    """O nome de cada campo da tabela, pelo id: dos campos lidos agora e do mapeamento guardado."""
    found: dict[str, str] = {}
    for field in state.get("fields") or []:
        if isinstance(field, dict) and field.get("name"):
            found[str(field.get("id"))] = str(field["name"])
    for entry in state.get("mapping") or []:
        if isinstance(entry, dict) and entry.get("name"):
            found.setdefault(str(entry.get("field_id")), str(entry["name"]))
    return found


def _entry_line(field_id: Any, name: Any, source: Any) -> str:
    label = f"{plain(name)} (#{plain(field_id)})" if name else f"#{plain(field_id)}"
    return f"{label} <- {plain(source)}"


def _entry_lines(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """`nome (#id) <- fonte` de cada campo, por inteiro (a pessoa confirma o que a planilha recebe)."""
    lines = [
        f"- {_entry_line(e.get('field_id'), e.get('name'), e.get('source'))}"
        for e in entries[:MAX_FIELDS_SHOWN]
    ]
    if len(entries) > MAX_FIELDS_SHOWN:
        lines.append(f"- ... e mais {len(entries) - MAX_FIELDS_SHOWN} campo(s).")
    return lines


def _source_lines(sources: Mapping[str, str], names: Mapping[str, str]) -> list[str]:
    return _entry_lines(
        [
            {"field_id": key, "name": names.get(key), "source": source}
            for key, source in sources.items()
        ]
    )


def _dict_entries(value: Any) -> list[Mapping[str, Any]]:
    return [e for e in value or [] if isinstance(e, dict)]


def _mapped_entries(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Os campos que a ativação passa a preencher: o mapeamento guardado (ou, sem ele, o relatório)."""
    kept = _dict_entries(state.get("mapping"))
    if kept:
        return kept
    report = state.get("report")
    fields = report.get("fields") if isinstance(report, dict) else None
    return [f for f in fields or [] if isinstance(f, dict)]


# --- a chamada ---------------------------------------------------------------------------------------


async def _connect_baserow_integration(steps: Steps, arguments: dict[str, Any]) -> ToolOutput:
    activate = flag(arguments, "activate")
    others = sorted(set(arguments) & {"base_url", "table_id", "token", "sources", "purpose"})
    if activate and others:
        return ToolOutput(
            "Ativar é um passo à parte: chame com só `slug` (e `activate: true`, e `confirm` se o "
            f"servidor pedir), sem {', '.join(f'`{n}`' for n in others)}. Nada foi feito.",
            is_error=True,
        )
    missing = [n for n in ("base_url", "table_id") if n not in arguments]
    if not activate and missing:
        return ToolOutput(
            "Para conectar faltam " + " e ".join(f"`{n}`" for n in missing) + ". Nada foi feito.",
            is_error=True,
        )
    if not activate and server_of(arguments["base_url"]) is None:
        return ToolOutput(
            "O `base_url` não tem um servidor que eu possa mostrar por inteiro à pessoa: precisa ser "
            "`https://servidor` (o servidor com até 253 caracteres, sem espaço nem caractere de "
            "controle). Nada foi feito.",
            is_error=True,
        )
    if not activate and arguments.get("purpose") not in (None, "internal"):
        return ToolOutput(
            "Baserow exige finalidade interna (`purpose: internal`): envia respostas e dados comerciais. Participante não é compatível; nada foi feito.",
            is_error=True,
        )
    slug = arguments["slug"]
    progress = Progress()
    reading = await steps.call("get_form_integration", {"slug": slug})
    if not succeeded(reading):
        return refused("ler a integração", reading, progress)
    state = _purpose_state(body(reading))
    if not state.get("published"):
        return ToolOutput(
            f"O formulário «{plain(slug)}» ainda não tem versão publicada, então não há perguntas para "
            "mapear. Publique antes (`publish_form_with_review`). Nada foi feito.",
            is_error=True,
        )
    if activate:
        return await _activate(steps, arguments, state, progress)
    return await _connect(steps, arguments, state, progress)


def _same_target(arguments: Mapping[str, Any], connection: Mapping[str, Any]) -> bool:
    asked = str(arguments["base_url"]).strip().rstrip("/")
    return (
        asked == str(connection.get("base_url"))
        and arguments["table_id"] == connection.get("table_id")
        and (arguments.get("purpose") or connection.get("purpose")) == connection.get("purpose")
    )


async def _connect(
    steps: Steps, arguments: dict[str, Any], state: dict[str, Any], progress: Progress
) -> ToolOutput:
    slug = arguments["slug"]
    connection = state.get("connection") or {}
    connected = bool(state.get("connected"))
    token = arguments.get("token")
    version = str(connection.get("version") or "0") if connected else "0"
    if not connected and not token:
        return ToolOutput(
            "Falta o `token` (o token de banco de dados do Baserow é obrigatório ao conectar pela "
            "primeira vez). Nada foi feito.",
            is_error=True,
        )
    same_url = connected and str(arguments["base_url"]).strip().rstrip("/") == str(
        connection.get("base_url")
    )
    if arguments.get("purpose") is None and (
        not same_url or connection.get("purpose") != "internal"
    ):
        return ToolOutput(
            "Informe `purpose: internal` explicitamente ao criar a conexão ou mudar a URL. Baserow recebe respostas e dados comerciais; nada foi feito.",
            is_error=True,
        )
    same = connected and _same_target(arguments, connection)
    if same and token is None and connection.get("token_unreadable"):
        return ToolOutput(
            "O token guardado não abre com a chave de hoje: mande o `token` de novo. Nada foi feito.",
            is_error=True,
        )
    explicit = _sources_of(arguments.get("sources"))
    stored = _stored_sources(state)
    # Conectada ao mesmo servidor e à mesma tabela, sem token novo, não há o que conectar.
    needs_connect = not (same and token is None)
    # O mapeamento guardado já é o pedido (a repetição de uma chamada que o guardou antes da conexão).
    already = bool(same and explicit is not None and explicit == stored)
    # Numa integração ativa e inalterada, a troca do mapeamento (que tem a confirmação própria) vem
    # antes da conexão: a repetição da chamada a pula, porque o mapeamento guardado já é o pedido.
    mapping_first = bool(same and connection.get("active") and explicit is not None and not already)
    names = _names(state)
    saved: Mapping[str, Any] | None = None
    deactivated = False
    if already:
        progress.done.append("O mapeamento guardado já é o que você mandou: nada a trocar.")
    mapping_done = already or mapping_first or not needs_connect

    if mapping_first or (not needs_connect and explicit is not None and not already):
        stopped, saved = await _store_mapping(
            steps,
            arguments,
            progress,
            version=arguments.get("expected_version") or version,
            sources=explicit or {},
            names=names,
            replaced=_dict_entries(state.get("mapping")),
            active=bool(connection.get("active")),
        )
        if stopped is not None:
            return stopped
        assert saved is not None
        version = str(saved.get("version"))
        deactivated = bool(saved.get("deactivated"))
    if not needs_connect:
        progress.done.append(
            f"Já conectada a {_shown_server(connection.get('base_url'))} (tabela "
            f"{plain(connection.get('table_id'))}): o token guardado fica, nada a conectar."
        )
        return ToolOutput(
            lines_of(
                ["Feito:", *(f"- {item}" for item in progress.done)],
                _mapping_lines(state, saved, {}, explicit is not None, mapping_done),
                _next_step(
                    slug,
                    saved or state,
                    version,
                    active=bool((saved or connection).get("active")),
                    deactivated=deactivated,
                ),
            )
        )

    sent: dict[str, Any] = {
        "slug": slug,
        "base_url": arguments["base_url"],
        "table_id": arguments["table_id"],
        "expected_version": version
        if saved is not None
        else arguments.get("expected_version") or version,
    }
    if "purpose" in arguments:
        sent["purpose"] = arguments["purpose"]
    if token is not None:
        sent["token"] = token
    sent.update(confirmations(arguments))
    # O que a pessoa vê junto com a conexão e a conexão não leva: o mapeamento que vem logo depois.
    bind = (
        {"sources": explicit, "replaces": stored}
        if explicit is not None and not mapping_first and not already
        else None
    )
    preface = _connect_preface(
        arguments, state, names, explicit, stored, mapping_before=mapping_first or already
    )
    connecting = await steps.call("connect_form_integration", sent, preface=preface, bind=bind)
    if connecting.awaiting:
        return waiting(
            [
                *(f"- {item}" for item in progress.done),
                *([""] if progress.done else []),
                preface,
                "",
                f"Integração de «{plain(slug)}» lida (versão {version}); nada foi conectado ainda.",
            ],
            connecting,
        )
    if not succeeded(connecting):
        return refused("conectar à tabela", connecting, progress, nothing="Nada foi gravado.")
    made = body(connecting)
    progress.done.append(
        f"Conectado à tabela {plain(made.get('table_id'))} em {plain(made.get('host'))} "
        f"(versão da integração {plain(made.get('version'))}; "
        f"{'ativa' if made.get('active') else 'desativada'})."
    )
    progress.not_done = ["guardar o mapeamento", "mostrar o relatório"] if not mapping_done else []
    rereading = await steps.call("get_form_integration", {"slug": slug})
    if not succeeded(rereading):
        return refused("ler a tabela depois de conectar", rereading, progress)
    current = _purpose_state(body(rereading))
    version = str((current.get("connection") or {}).get("version") or made.get("version"))
    names = _names(current)
    suggestion: dict[str, str] = {}
    sources: Mapping[str, str] | None = explicit
    if not mapping_done:
        if explicit is None and not current.get("mapping"):
            suggestion = _suggested(current.get("fields") or [])
            sources = (
                suggestion
                if _has_one_key(suggestion, profile=current.get("profile", "pdf"))
                else None
            )
        # Com um mapeamento guardado e sem `sources`, a conexão o manteve (só o token mudou): não o
        # troco por um palpite.
        if sources is not None:
            stopped, saved = await _store_mapping(
                steps,
                arguments,
                progress,
                version=version,
                sources=sources,
                names=names,
                replaced=_dict_entries(current.get("mapping")),
                active=bool((current.get("connection") or {}).get("active")),
            )
            if stopped is not None:
                return stopped
            assert saved is not None
            version = str(saved.get("version"))
            deactivated = bool(saved.get("deactivated"))
    last = saved if saved is not None and not mapping_first else None
    active = bool((last or {}).get("active", (current.get("connection") or {}).get("active")))
    return ToolOutput(
        lines_of(
            ["Feito:", *(f"- {item}" for item in progress.done)],
            _mapping_lines(current, last, suggestion, explicit is not None, mapping_done),
            _next_step(slug, last or current, version, active=active, deactivated=deactivated),
        ),
        status=connecting.status,
    )


async def _store_mapping(
    steps: Steps,
    arguments: Mapping[str, Any],
    progress: Progress,
    *,
    version: Any,
    sources: Mapping[str, str],
    names: Mapping[str, str],
    replaced: Sequence[Mapping[str, Any]],
    active: bool,
) -> tuple[ToolOutput | None, Mapping[str, Any] | None]:
    """Guarda o mapeamento (`save_form_integration_mapping`, que a API não marca). Numa integração
    **ativa**, antes pede a confirmação própria da pessoa, com o que há hoje e o que passa a haver.
    Devolve `(retorno que encerra a composta, None)` ou `(None, o mapeamento guardado)`."""
    slug = arguments["slug"]
    if active:
        question = _mapping_question(slug, sources, names, replaced)
        bound = {
            "slug": slug,
            "expected_version": str(version),
            "sources": dict(sources),
            "replaces": {str(e.get("field_id")): str(e.get("source")) for e in replaced},
        }
        gate = await steps.gate(
            "mapping", bound, question=question, model_says_yes=arguments.get("confirm") is True
        )
        if gate.answer == "no":
            stopped = refused(
                "trocar o mapeamento",
                ToolOutput(gate.text, is_error=True, confirmation=gate.outcome),
                progress,
                nothing="O mapeamento não foi trocado.",
            )
            return stopped, None
        if gate.answer == "wait":
            done = [f"- {item}" for item in progress.done]
            return (
                waiting(
                    [*done, *([""] if done else []), "Falta a confirmação do mapeamento."],
                    ToolOutput(gate.text, awaiting=True),
                ),
                None,
            )
    storing = await steps.call(
        "save_form_integration_mapping",
        {"slug": slug, "expected_version": str(version), "sources": dict(sources)},
    )
    if not succeeded(storing):
        return (
            refused(
                "guardar o mapeamento",
                storing,
                progress,
                nothing="O mapeamento não foi gravado."
                + (" A conexão foi." if any("Conectado" in item for item in progress.done) else ""),
            ),
            None,
        )
    saved = body(storing)
    progress.done.append(f"Mapeamento guardado: {len(saved.get('entries') or [])} campo(s).")
    return None, saved


def _mapping_question(
    slug: Any,
    sources: Mapping[str, str],
    names: Mapping[str, str],
    replaced: Sequence[Mapping[str, Any]],
) -> str:
    today = _entry_lines(replaced) or ["- (nenhum campo)"]
    return "\n".join(
        [
            (
                f"Pedido de `connect_baserow_integration`: trocar o mapeamento de «{plain(slug)}». A "
                "integração está ATIVA: o próximo pedido concluído já preenche a planilha do cliente pelo "
                "mapeamento novo."
            ),
            f"Hoje ({len(replaced)} campo(s)):",
            *today,
            f"Passa a ser ({len(sources)} campo(s)):",
            *_source_lines(sources, names),
            (
                "Confirma «Guardar o mapeamento da integração com o Baserow» "
                "(`save_form_integration_mapping`, que a API não marca)?"
            ),
        ]
    )


def _suggested(fields: Sequence[Any]) -> dict[str, str]:
    """A fonte que o nome de cada campo sugere (a API só a dá enquanto nada está mapeado)."""
    return {
        str(field["id"]): field["suggested_source"]
        for field in fields
        if isinstance(field, dict)
        and field.get("suggested_source")
        and not field.get("unsupported")
    }


def _has_one_key(sources: Mapping[str, str], *, profile: str = "pdf") -> bool:
    """O mapeamento precisa de um campo, e só um, com a chave do perfil."""
    return (
        list(sources.values()).count("submission.id" if profile == "capture" else "job.public_id")
        == 1
    )


def _mapping_lines(
    current: Mapping[str, Any],
    saved: Mapping[str, Any] | None,
    suggestion: Mapping[str, str],
    explicit: bool,
    mapping_done: bool,
) -> list[str]:
    if saved is not None:
        origin = "o que você mandou" if explicit else "a sugestão pelo nome dos campos"
        entries = [e for e in saved.get("entries") or [] if isinstance(e, dict)]
        return [
            f"Mapeamento ({origin}):",
            *_entry_lines(entries),
            *_report_lines(saved.get("report")),
        ]
    if mapping_done and explicit:
        # O mapeamento foi guardado (ou já era o pedido) antes da conexão: o que vale é o de agora.
        entries = [e for e in current.get("mapping") or [] if isinstance(e, dict)]
        return [
            "Mapeamento (o que você mandou):",
            *_entry_lines(entries),
            *_report_lines(current.get("report")),
        ]
    if current.get("fields_error"):
        return [
            (
                "NÃO guardei o mapeamento: não consegui ler a tabela do Baserow agora "
                f"({plain(current['fields_error'], 300)}). Tente de novo (a conexão já está gravada)."
            )
        ]
    kept = current.get("mapping") or []
    if kept:
        return [
            f"Mapeamento mantido ({len(kept)} campo(s)): `sources` não veio, então não o troquei.",
            *_report_lines(current.get("report")),
        ]
    if suggestion:
        return [
            (
                "NÃO guardei o mapeamento: a sugestão pelo nome não acha uma única chave do perfil "
                "(submission.id na captura; job.public_id no PDF), que a API exige."
            ),
            "Sugestão pelo nome dos campos:",
            *listed(
                [f"#{field_id} <- {source}" for field_id, source in sorted(suggestion.items())]
            ),
            (
                "Escolha as fontes (`fields` de `get_form_integration` traz as de cada campo) e chame de "
                "novo com `sources`."
            ),
        ]
    return [
        (
            "NÃO guardei o mapeamento: o nome dos campos da tabela não sugere nenhuma fonte. Escolha as "
            "fontes (`fields` de `get_form_integration`) e chame de novo com `sources`."
        )
    ]


def _report_lines(report: Any) -> list[str]:
    if not isinstance(report, dict):
        return ["Relatório: não há (nada mapeado, ou a tabela não foi lida)."]
    fields = report.get("fields") or []
    return [
        (
            "Relatório do mapeamento contra a tabela de agora: "
            f"{'LIMPO' if report.get('ok') else 'COM PENDÊNCIAS'} ({len(fields)} campo(s) mapeado(s), "
            f"{plain(report.get('unmatched_options', 0))} opção(ões) do formulário sem par no Baserow)."
        ),
        *listed([plain(p, 300) for p in report.get("problems") or []]),
    ]


def _next_step(
    slug: Any, found: Mapping[str, Any], version: str, *, active: bool, deactivated: bool
) -> list[str]:
    report = found.get("report")
    clean = isinstance(report, dict) and report.get("ok") is True
    if deactivated:
        return [
            (
                "A integração ESTAVA ativa e foi DESATIVADA: o relatório do mapeamento novo tem pendências, "
                "e uma integração com pendências não segue enviando. Corrija a tabela ou o mapeamento "
                f"(`sources`) e ative de novo. Versão da integração: {version}."
            )
        ]
    if active and clean:
        return [
            (
                "A integração está ATIVA e segue gravando uma linha por pedido concluído (só o token "
                "mudou, ou o mapeamento foi trocado sem pendências)."
            )
        ]
    if active:
        return [
            (
                "A integração está ATIVA, mas o relatório tem pendências (acima): corrija a tabela ou o "
                f"mapeamento (`sources`). Versão da integração: {version}."
            )
        ]
    if clean:
        return [
            (
                "A integração segue DESATIVADA. Para ativar (cada pedido concluído vira uma linha na "
                "planilha do cliente): mostre o relatório à pessoa e chame `connect_baserow_integration` "
                f"com `slug: {plain(slug)}` e `activate: true`; o servidor pergunta a ela antes."
            )
        ]
    return [
        (
            "A integração segue DESATIVADA e ainda não pode ser ativada. Corrija as pendências acima "
            "(a tabela no Baserow, ou o mapeamento com `sources`) e chame de novo; ela só ativa com o "
            f"relatório limpo. Versão da integração: {version}."
        )
    ]


def _connect_preface(
    arguments: Mapping[str, Any],
    state: Mapping[str, Any],
    names: Mapping[str, str],
    explicit: Mapping[str, str] | None,
    stored: Mapping[str, str],
    *,
    mapping_before: bool,
) -> str:
    """O que a pessoa precisa ver para confirmar a conexão: o servidor por inteiro, o que a conexão faz
    com a integração de hoje e o mapeamento que vem logo depois (o que `sources` guarda e o que ele troca).
    É a pergunta da *elicitation* e, sem ela, a primeira parte do texto que espera a chamada seguinte."""
    connection = state.get("connection") or {}
    later = "ativar é outro passo, com outra confirmação."
    same = bool(state.get("connected")) and _same_target(arguments, connection)
    if not state.get("connected"):
        now = f"Hoje o formulário não tem integração. A integração nasce desativada: {later}"
    else:
        now = (
            f"Hoje está conectado a {_shown_server(connection.get('base_url'))} (tabela "
            f"{plain(connection.get('table_id'))}), "
            f"{'ativa' if connection.get('active') else 'desativada'}, com "
            f"{plain(connection.get('mapped_fields', 0))} campo(s) mapeado(s)."
        )
        if arguments.get("token") is not None:
            now += " O token guardado será trocado pelo que veio."
        if not same:
            now += (
                f" Mudar o endereço ou a tabela APAGA o mapeamento e desativa a integração; {later}"
            )
        elif connection.get("active"):
            now += " A integração continua ativa."
        else:
            now += f" A integração continua desativada: {later}"
    lines = [
        (
            f"Pedido de `connect_baserow_integration`: conectar «{plain(arguments['slug'])}» ao servidor "
            f"{_shown_server(arguments['base_url'])}, tabela {plain(arguments['table_id'])}; o token vai a "
            f"esse servidor. Finalidade interna: respostas e dados comerciais. {now}"
        )
    ]
    if explicit is not None and not mapping_before:
        wiped = state.get("connected") and not same
        lines.append(f"Em seguida guarda o mapeamento que você mandou ({len(explicit)} campo(s)):")
        lines += _source_lines(explicit, names)
        if stored:
            lines.append(
                f"{'O mapeamento de hoje, apagado pela conexão' if wiped else 'Troca o mapeamento de hoje'} "
                f"({len(stored)} campo(s)):"
            )
            lines += _source_lines(stored, names)
    elif explicit is None:
        lines.append(
            "Sem `sources`: se nada estiver mapeado, guarda a sugestão pelo nome dos campos (o texto "
            "dela vem depois); um mapeamento que existe é mantido."
        )
    return "\n".join(lines)


async def _activate(
    steps: Steps, arguments: dict[str, Any], state: dict[str, Any], progress: Progress
) -> ToolOutput:
    slug = arguments["slug"]
    connection = state.get("connection") or {}
    if not state.get("connected"):
        return ToolOutput(
            f"O formulário «{plain(slug)}» não tem integração: conecte primeiro "
            "(`connect_baserow_integration` com `base_url`, `table_id` e `token`). Nada foi feito.",
            is_error=True,
        )
    version = str(connection.get("version"))
    seen = arguments.get("expected_version")
    if seen is not None and seen != version:
        return ToolOutput(
            f"A integração mudou desde a versão que você viu ({plain(seen)} -> {version}). Releia o "
            "relatório com `get_form_integration`, mostre à pessoa e repita com a versão de agora. "
            "Nada foi ativado.",
            is_error=True,
        )
    if connection.get("token_unreadable"):
        return ToolOutput(
            "O token guardado não abre com a chave de hoje: mande o token de novo (conecte outra vez) "
            "antes de ativar. Nada foi ativado.",
            is_error=True,
        )
    if state.get("fields_error"):
        return ToolOutput(
            f"Não consegui ler a tabela do Baserow ({plain(state['fields_error'], 300)}), então não "
            "há relatório para conferir. Nada foi ativado.",
            is_error=True,
        )
    report = state.get("report")
    shown = _report_lines(report)
    if connection.get("active"):
        return ToolOutput(lines_of(["A integração já está ativa. Nada a fazer."], shown))
    if not isinstance(report, dict) or report.get("ok") is not True:
        return ToolOutput(
            lines_of(
                shown,
                [
                    (
                        "NÃO ativei: o relatório tem pendências (ou nada está mapeado). Corrija a tabela "
                        "ou o mapeamento (`connect_baserow_integration` com `sources`) e tente de novo."
                    )
                ],
            ),
            is_error=True,
        )
    sent: dict[str, Any] = {"slug": slug, "expected_version": version}
    sent.update(confirmations(arguments))
    preface = _activate_preface(slug, connection, state, report)
    activating = await steps.call("activate_form_integration", sent, preface=preface)
    if activating.awaiting:
        return waiting(
            [
                preface,
                "",
                f"Integração de «{plain(slug)}» lida (versão {version}); o relatório está limpo.",
                *shown,
            ],
            activating,
        )
    if not succeeded(activating):
        return refused("ativar a integração", activating, progress, nothing="Nada foi ativado.")
    return ToolOutput(
        lines_of(
            [
                (
                    f"ATIVADA: a integração de «{plain(slug)}» com a tabela "
                    f"{plain(connection.get('table_id'))} ({plain(connection.get('host'), 260)}) agora "
                    "grava uma linha por pedido concluído. Os pedidos que já estavam prontos não são "
                    "sincronizados (reprocesse-os, se preciso)."
                ),
            ],
            shown,
        ),
        status=activating.status,
    )


def _activate_preface(
    slug: Any, connection: Mapping[str, Any], state: Mapping[str, Any], report: Mapping[str, Any]
) -> str:
    """O que a pessoa confirma ao ativar: a tabela, o servidor e **cada campo** que passa a ser preenchido,
    com a fonte (`nome (#id) <- fonte`), e o relatório. É a pergunta da *elicitation* e, sem ela, a
    primeira parte do texto que espera a chamada seguinte."""
    entries = _mapped_entries(state)
    return "\n".join(
        [
            (
                f"Pedido de `connect_baserow_integration`: ativar a integração de «{plain(slug)}» com a "
                f"tabela {plain(connection.get('table_id'))} do Baserow em "
                f"{plain(connection.get('host'), 260)}. A partir daqui, cada pedido concluído do formulário "
                "vira uma linha na planilha do cliente, e estes campos passam a ser preenchidos:"
            ),
            *_entry_lines(entries),
            (
                f"Relatório do mapeamento: {len(report.get('fields') or [])} campo(s) mapeado(s), sem "
                f"pendências, {plain(report.get('unmatched_options', 0))} opção(ões) sem par."
            ),
        ]
    )
