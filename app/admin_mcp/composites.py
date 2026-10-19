"""As cinco ferramentas compostas do MCP de admin (ADR 0015, "Decisões da fase 9.2"; issue #114).

Cada uma junta chamadas de ferramentas geradas e **só delas** (`compose.py` diz como e por quê):

| composta                      | chama                                                              | ação marcada que pode disparar                   |
| ----------------------------- | ------------------------------------------------------------------ | ------------------------------------------------ |
| `attention_overview`          | `list_jobs`, `list_destinations`, `list_forms`, `get_form_integration` | nenhuma (só lê)                              |
| `diagnose_order`              | `get_job` (o conserto ela só **sugere**)                           | nenhuma (só lê)                                  |
| `edit_form_draft`             | `get_form_draft`, `patch_form_draft`, `compare_form_versions`      | nenhuma (grava o rascunho, que a API não marca)  |
| `publish_form_with_review`    | `review_form_draft_publication`, `publish_form_draft`              | `publish_form_draft`, só com `publish: true`     |
| `connect_baserow_integration` | `get_form_integration`, `connect_form_integration`, `save_form_integration_mapping`, `activate_form_integration` | `connect_form_integration`; `activate_form_integration` só com `activate: true` |

**A confirmação é a do servidor, uma por ação.** Nenhuma composta escreve `confirm`: as que podem
disparar uma ação marcada repassam o `confirm` e o `confirm_contact` que o modelo mandou a **uma** chamada
gerada, e o `ToolRunner` decide se valem (com *elicitation* ele pergunta e ignora o que o modelo
escreveu; sem ela, só vale para uma chamada idêntica que ele já mostrou à pessoa). Para a pessoa ver o
que está confirmando, a composta passa à pergunta um `preface` com o que ela levantou (as mudanças que a
publicação faria, o servidor do Baserow e o mapeamento, cada campo que a ativação preenche), e o mesmo
texto abre o retorno que espera a chamada seguinte, para o cliente sem *elicitation* também o mostrar.
Cada ação marcada é um passo à parte: publicar só com `publish: true`, ativar só com `activate: true` e
sozinho, nunca no mesmo passo de outra ação marcada. O que a pessoa viu e a chamada não leva (o mapeamento
que vem logo depois da conexão) entra na impressão digital da confirmação (`bind`), e guardar o mapeamento
de uma integração ativa, que a API não marca, tem a confirmação própria (`Steps.gate`). Assim a
confirmação cobre exatamente a ação que o texto mostrou.

**Dado da lead.** Nenhuma chama `download_job_pdf` nem pede `q`; só `diagnose_order` tem
`include_lead_data`, e só quando a ferramenta gerada `get_job` o tem (a opção `GERADOR_MCP_LEAD_DATA` e uma
chave `total`); mesmo então, imprime apenas os textos livres dos erros, nunca o nome, o e-mail nem as
respostas da lead.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.admin_mcp.catalog import ToolSpec
from app.admin_mcp.compose import Builder, Composite, available
from app.admin_mcp.composites_baserow import connect_baserow_integration
from app.admin_mcp.composites_forms import edit_form_draft, publish_form_with_review
from app.admin_mcp.composites_orders import attention_overview, diagnose_order

BUILDERS: tuple[Builder, ...] = (
    attention_overview,
    diagnose_order,
    edit_form_draft,
    publish_form_with_review,
    connect_baserow_integration,
)


def for_tools(tools: Mapping[str, ToolSpec]) -> list[Composite]:
    """As compostas desta chave: as que têm todas as operações que chamam registradas."""
    return available(tools, BUILDERS)
