"""As compostas do rascunho: `edit_form_draft` e `publish_form_with_review`.

Ver `composites.py` (o quadro das cinco) e `compose.py` (as regras).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.admin_mcp.catalog import ToolSpec
from app.admin_mcp.compose import (
    Composite,
    Progress,
    Steps,
    body,
    borrowed,
    build,
    confirmations,
    first_line,
    flag,
    lines_of,
    listed,
    plain,
    refused,
    succeeded,
    waiting,
)
from app.admin_mcp.results import ToolOutput

MAX_DIFF_LINES = 20  # linhas de diferença por seção no texto de `edit_form_draft`
MAX_DIFF_TOTAL = 80
MAX_NOTE = 300


# =================================================================================================
# edit_form_draft
# =================================================================================================


def edit_form_draft(tools: Mapping[str, ToolSpec]) -> Composite | None:
    calls = ("get_form_draft", "patch_form_draft", "compare_form_versions")
    if not all(call in tools for call in calls):
        return None
    shared = borrowed(tools, "patch_form_draft", "slug", "patch")
    properties = dict(shared["properties"])
    properties["expected_version"] = {
        "type": "string",
        "description": "A `version` do rascunho que você viu (`get_form_draft`), se o patch foi "
        "escrito sobre um documento que você leu antes (os caminhos têm índices). Se o rascunho já "
        "é outro, a API responde `409 version_conflict`, com a versão de agora, e nada é gravado. "
        "Sem ela, o patch vale para o rascunho de agora (a versão que esta ferramenta acabou de ler).",
    }
    return build(
        tools,
        name="edit_form_draft",
        summary="Editar o rascunho de um formulário e ver o que mudou",
        details=(
            "Lê o rascunho (`get_form_draft`), aplica um JSON Patch (`patch_form_draft`: tudo ou nada, "
            "e grava o rascunho na hora) e compara o rascunho com a versão publicada "
            "(`compare_form_versions`), mostrando as seções que mudam e as linhas. **Não publica nada**: "
            "para publicar, `publish_form_with_review`. A API não marca a gravação do rascunho como ação "
            "que pede confirmação, e esta ferramenta também não pergunta; use `expected_version` quando o "
            "patch depender de um documento que você leu antes. Se a gravação der certo e só a "
            "comparação falhar, o texto diz que o rascunho foi gravado: não repita o patch."
        ),
        calls=calls,
        properties=properties,
        required=("slug", "patch"),
        definitions=shared["$defs"],
        run=_edit_form_draft,
    )


async def _edit_form_draft(steps: Steps, arguments: dict[str, Any]) -> ToolOutput:
    slug = arguments["slug"]
    progress = Progress()
    reading = await steps.call("get_form_draft", {"slug": slug})
    if not succeeded(reading):
        return refused("ler o rascunho", reading, progress)
    seen = body(reading).get("version")
    patched = await steps.call(
        "patch_form_draft",
        {
            "slug": slug,
            "expected_version": arguments.get("expected_version") or seen,
            "patch": arguments["patch"],
        },
    )
    if not succeeded(patched):
        return refused(
            "gravar o patch no rascunho",
            patched,
            progress,
            nothing="Nada foi gravado no rascunho (o patch é tudo ou nada).",
        )
    saved = body(patched)  # o documento inteiro vem junto, e fica de fora do texto
    current = saved.get("current_version")
    report = [
        f"Rascunho de «{plain(slug)}» gravado. NADA foi publicado.",
        f"Versão do rascunho: {plain(saved.get('version'))} (antes: {plain(seen)}). "
        f"Pendente de publicação: {'sim' if saved.get('pending') else 'não'}."
        + (f" Salvo por {plain(saved['saved_by'])}." if saved.get("saved_by") else ""),
    ]
    if saved.get("stale_assets"):
        report.append(
            "Imagens trocadas desde a versão publicada: "
            + ", ".join(plain(name, 60) for name in saved["stale_assets"])
        )
    if current is None:
        report.append(
            "O formulário ainda não tem versão publicada: não há com o que comparar. A primeira "
            "publicação cria a versão 1."
        )
        return ToolOutput(lines_of(report, _publish_hint(slug)), status=patched.status)
    comparing = await steps.call(
        "compare_form_versions", {"slug": slug, "from": str(current), "to": "draft"}
    )
    if not succeeded(comparing):
        report += [
            (
                f"A comparação com a versão {plain(current)} FALHOU, mas o rascunho foi gravado (não "
                f"repita o patch): {first_line(comparing)}"
            ),
            "Compare à mão com `compare_form_versions` (`from` e `to: draft`).",
        ]
        return ToolOutput(lines_of(report, _publish_hint(slug)), status=patched.status)
    return ToolOutput(
        lines_of(report, _comparison(body(comparing), current), _publish_hint(slug)),
        status=patched.status,
    )


def _comparison(found: Mapping[str, Any], current: Any) -> list[str]:
    changed = [s for s in found.get("sections") or [] if isinstance(s, dict) and s.get("changed")]
    if not changed:
        return [
            f"Contra a versão publicada {plain(current)}: o rascunho está igual (nenhuma seção mudou)."
        ]
    lines = [
        (
            f"Contra a versão publicada {plain(current)} (inclui o que já estava pendente antes deste "
            f"patch): {len(changed)} seção(ões) mudam."
        )
    ]
    shown = 0
    cut = False
    for section in changed:
        diff = [
            line
            for line in section.get("lines") or []
            if isinstance(line, dict) and line.get("kind") in ("add", "del")
        ]
        added = sum(1 for line in diff if line["kind"] == "add")
        lines.append(f"- {plain(section.get('title'))}: +{added} -{len(diff) - added}")
        room = max(min(MAX_DIFF_LINES, MAX_DIFF_TOTAL - shown), 0)
        lines += [f"    {plain(line.get('text'), 160)}" for line in diff[:room]]
        shown += min(len(diff), room)
        cut = cut or len(diff) > room
    if cut:
        lines.append("    ... (há mais linhas: `compare_form_versions` traz todas)")
    return lines


def _publish_hint(slug: Any) -> list[str]:
    return [f"Para publicar: `publish_form_with_review` com `slug: {plain(slug)}` e uma `note`."]


# =================================================================================================
# publish_form_with_review
# =================================================================================================


def publish_form_with_review(tools: Mapping[str, ToolSpec]) -> Composite | None:
    calls = ("review_form_draft_publication", "publish_form_draft")
    if not all(call in tools for call in calls):
        return None
    properties = borrowed(
        tools, "publish_form_draft", "slug", "note", "confirm", "confirm_contact"
    )["properties"]
    properties["publish"] = {
        "type": "boolean",
        "default": False,
        "description": "`true`: depois de simular e mostrar o que muda, publica (a pessoa confirma "
        "antes, pelo servidor MCP). `false` (o padrão): só simula e mostra o que muda e o que falta, "
        "sem publicar.",
    }
    properties["expected_version"] = {
        "type": "string",
        "description": "A `version` do rascunho que você mostrou à pessoa (vem no texto desta "
        "ferramenta). Se o rascunho já é outro, a ferramenta para antes de simular e de publicar e diz "
        "a versão de agora, para a confirmação nunca cobrir um rascunho que a pessoa não viu.",
    }
    return build(
        tools,
        name="publish_form_with_review",
        summary="Revisar, simular e publicar o rascunho de um formulário",
        details=(
            "Lê a revisão (`review_form_draft_publication`: as seções que mudam, o teste do que vai à "
            "IA, os dados de contato no prompt), simula a publicação (`publish_form_draft` com "
            "`dry_run: true`, que gera o PDF de exemplo, não grava e não pede confirmação) e mostra o que "
            "muda e o que falta. **Só publica com `publish: true`**, com a nota, e só se a simulação "
            "diz que nada falta; então chama `publish_form_draft` de verdade, que **muda o que a lead "
            "vê** (vale já para os envios novos) e é uma ação marcada: o servidor pergunta à pessoa, "
            "mostrando o que muda, ou, sem como perguntar, devolve a descrição e a publicação só "
            "executa quando você repetir esta ferramenta, com os mesmos argumentos, `expected_version` "
            "e `confirm: true` depois do sim dela. Se o prompt cita nome, e-mail ou telefone da lead, a "
            "API pede a segunda confirmação (`confirm_contact`), mostrada do mesmo jeito. A versão que "
            "a simulação leu é a que se publica: se o rascunho mudar no meio, a API recusa (409) e "
            "nada é publicado."
        ),
        calls=calls,
        properties=properties,
        required=("slug",),
        run=_publish_form_with_review,
    )


async def _publish_form_with_review(steps: Steps, arguments: dict[str, Any]) -> ToolOutput:
    slug = arguments["slug"]
    note = arguments.get("note")
    publish = flag(arguments, "publish")
    progress = Progress()
    reviewing = await steps.call("review_form_draft_publication", {"slug": slug})
    if not succeeded(reviewing):
        return refused("revisar o que a publicação faria", reviewing, progress)
    review = body(reviewing)
    version = review.get("version")
    changes = _review_lines(review)
    on_review = _review_lines(review, with_missing=True)
    seen = arguments.get("expected_version")
    if seen is not None and seen != version:
        return ToolOutput(
            lines_of(
                [
                    (
                        f"O rascunho de «{plain(slug)}» mudou desde a versão que você viu "
                        f"({plain(seen)} -> {plain(version)}). Nada foi simulado nem publicado."
                    ),
                    (
                        "Mostre à pessoa o que há agora (abaixo) e repita com "
                        f"`expected_version: {plain(version)}`."
                    ),
                ],
                on_review,
            ),
            is_error=True,
        )
    if review.get("nothing_to_publish"):
        return ToolOutput(
            lines_of(on_review, ["Não há o que publicar: o rascunho é igual à versão publicada."]),
            is_error=publish,
        )
    asked: dict[str, Any] = {"slug": slug, "dry_run": True, "expected_version": version}
    if note:
        asked["note"] = note
    simulating = await steps.call("publish_form_draft", asked)
    if not succeeded(simulating):
        return refused(
            "simular a publicação", simulating, progress, nothing="A simulação não grava nada."
        )
    inner = body(simulating).get("review") or {}
    if inner.get("version") != version:
        return ToolOutput(
            lines_of(
                changes,
                [
                    (
                        "O rascunho mudou durante a simulação: nada foi publicado. Repita a ferramenta "
                        "para revisar o rascunho de agora."
                    )
                ],
            ),
            is_error=True,
        )
    missing = [plain(item, 400) for item in inner.get("missing") or []]
    # A confirmação dos dados de contato não impede publicar: a API a pede na publicação, e o
    # servidor a mostra à pessoa. As outras faltas (nota, teste, PDF que não sai) impedem.
    contact = [item for item in missing if "confirm_contact" in item]
    blocking = [item for item in missing if "confirm_contact" not in item]
    blocking += [
        f"{plain(p.get('field') or 'definição')}: {plain(p.get('message'), 300)}"
        for p in inner.get("problems") or []
        if isinstance(p, dict)
    ]
    if publish and not (isinstance(note, str) and note.strip()) and not blocking:
        blocking.append(
            "Escreva uma nota do que mudou (`note`): ela fica no histórico, com a versão."
        )
    verdict = ["Simulação (com o PDF de exemplo, sem gravar nada):"]
    verdict += (
        listed(["FALTA: " + item for item in blocking]) if blocking else ["- pronta: nada falta."]
    )
    if contact:
        verdict += listed(
            ["A publicação vai pedir mais uma confirmação: " + item for item in contact]
        )
    if blocking and publish:
        return ToolOutput(
            lines_of(changes, verdict, ["NÃO publiquei: falta o que está acima."]), is_error=True
        )
    if not publish:
        return ToolOutput(
            lines_of(changes, verdict, _how_to_publish(slug, version, note, bool(contact)))
        )
    sent: dict[str, Any] = {"slug": slug, "expected_version": version, "note": note}
    sent.update(confirmations(arguments))
    preface = _publish_preface(slug, review, note, bool(contact))
    publishing = await steps.call("publish_form_draft", sent, preface=preface)
    if publishing.awaiting:
        return waiting(
            [
                preface,
                "",
                lines_of(changes, verdict),
                "",
                (
                    f"Na repetição, acrescente `expected_version: {plain(version)}`: a confirmação "
                    "vale para esta versão do rascunho, e se ele mudar a ferramenta para e avisa."
                ),
            ],
            publishing,
        )
    if not succeeded(publishing):
        return refused("publicar", publishing, progress, nothing="Nada foi publicado.")
    created = body(publishing).get("published_version") or {}
    changed = [c for c in review.get("changes") or [] if isinstance(c, dict) and c.get("changed")]
    return ToolOutput(
        lines_of(
            [
                (
                    f"PUBLICADO: «{plain(slug)}» agora está na versão {plain(created.get('number'))}, "
                    f"por {plain(created.get('published_by'))}. Vale já para os envios novos; os pedidos "
                    "que já existem seguem a versão deles."
                ),
                f"Nota: {plain(created.get('note'), MAX_NOTE)}",
                "Seções que mudaram: "
                + (", ".join(plain(c.get("title"), 60) for c in changed) or "(a primeira versão)")
                + ".",
            ]
        ),
        status=publishing.status,
    )


def _review_lines(review: Mapping[str, Any], *, with_missing: bool = False) -> list[str]:
    """O que a revisão diz: a versão que seria criada, as seções que mudam, o teste do que vai à IA, os
    dados de contato, o consentimento e o que muda para quem recebe. `with_missing` acrescenta o que
    falta segundo a revisão (quando a simulação não roda, é só o que há; quando roda, ela diz tudo)."""
    number = plain(review.get("number"))
    if review.get("first_publication"):
        head = f"Publicar criaria a versão {number}, a primeira: ela põe o formulário no ar."
    else:
        head = f"Publicar criaria a versão {number} (hoje: {plain(review.get('current_version'))})."
    lines = [f"{head} Versão do rascunho revisado: `{plain(review.get('version'))}`."]
    changed = [c for c in review.get("changes") or [] if isinstance(c, dict) and c.get("changed")]
    if changed:
        lines.append(
            "Seções que mudam: " + ", ".join(plain(c.get("title"), 60) for c in changed) + "."
        )
    elif not review.get("first_publication"):
        lines.append("Nenhuma seção do texto mudou.")
    test = review.get("test") or {}
    status = test.get("status")
    if status and status != "not_needed":
        said = (
            "coberto por um teste com sucesso"
            if status == "covered"
            else "FALTA um teste com sucesso"
        )
        changed_llm = ", ".join(plain(x, 60) for x in test.get("llm_changes") or [])
        lines.append(f"O que vai à IA mudou ({changed_llm}): {said} (`start_form_draft_test`).")
    contact = [plain(x, 20) for x in review.get("contact") or []]
    if contact:
        lines.append(
            "O prompt manda dados de contato da lead à IA em todo pedido: "
            + ", ".join(contact)
            + "."
        )
    if review.get("consent_version"):
        lines.append(
            f"A versão do consentimento sobe para {plain(review['consent_version'])} (quem está com a "
            "página aberta revisa o aviso e concorda pela caixa ou ao enviar, conforme o método da nova versão)."
        )
    dropped = [d for d in review.get("dropped_values") or [] if isinstance(d, dict)]
    if dropped:
        lines += [
            f"Valores de opção que os envios novos deixam de trazer ({len(dropped)}):",
            *listed(
                [
                    f"{plain(d.get('question'))}: «{plain(d.get('option'))}» ({plain(d.get('value'))})"
                    for d in dropped
                ],
                10,
            ),
        ]
    if review.get("stale_assets"):
        lines.append(
            "Imagens trocadas desde a versão atual (a publicação fixa o conteúdo de agora): "
            + ", ".join(plain(name, 60) for name in review["stale_assets"])
        )
    missing = [plain(item, 300) for item in review.get("missing") or []]
    if with_missing and missing:
        lines += ["Falta, segundo a revisão:", *listed(missing)]
    return lines


def _how_to_publish(slug: Any, version: Any, note: Any, contact: bool) -> list[str]:
    note_part = (
        f"`note: {plain(note, MAX_NOTE)}`"
        if note
        else "uma `note` (obrigatória, até 300 caracteres)"
    )
    more = (
        " e `confirm_contact: true` (só depois do sim dela à segunda pergunta)" if contact else ""
    )
    return [
        (
            "Para publicar: mostre à pessoa o que muda (acima) e chame `publish_form_with_review` com "
            f"`slug: {plain(slug)}`, {note_part}, `publish: true` e `expected_version: {plain(version)}`; "
            "o servidor pergunta a ela antes de publicar. Sem como perguntar, ele devolve a descrição e "
            f"você repete a chamada com `confirm: true`{more}."
        )
    ]


def _publish_preface(slug: Any, review: Mapping[str, Any], note: Any, contact: bool) -> str:
    changed = [c for c in review.get("changes") or [] if isinstance(c, dict) and c.get("changed")]
    parts = [
        (
            f"Pedido de `publish_form_with_review`: publicar o rascunho de «{plain(slug)}» como a versão "
            f"{plain(review.get('number'))}, que vale já para os envios novos."
        ),
        "Muda: "
        + (
            ", ".join(plain(c.get("title"), 60) for c in changed)
            if changed
            else "a primeira versão"
        )
        + ".",
        f"Nota da publicação: {plain(note, MAX_NOTE)}",
        "A simulação, com o PDF de exemplo, não achou o que impeça.",
    ]
    if (review.get("test") or {}).get("status") == "covered":
        parts.append("O que vai à IA foi coberto por um teste com sucesso.")
    if contact:
        parts.append(
            "O prompt manda dados de contato da lead à IA (a API vai pedir mais uma confirmação)."
        )
    return " ".join(parts)
