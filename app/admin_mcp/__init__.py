"""O MCP da API de admin: um programa à parte (`python -m app.admin_mcp`), cliente HTTP da API.

Ele fala MCP por stdio com o Claude Code ou o Claude Desktop da operadora e, do outro lado, chama a API
de admin (`/admin/api/v1`) com a chave de quem o roda. **Não importa o app** (nem `app.main`, nem os
modelos, o banco ou as configurações) e não toca o banco: a autorização é a da API, e nada novo sobe no
servidor. Por isso este módulo só tem a docstring; um teste importa o pacote num processo à parte e
confere que nenhum módulo do app foi carregado (`tests_app/test_admin_mcp_isolation.py`).

As peças, da borda para o miolo:

- `config`: o ambiente (`GERADOR_API_URL`, `GERADOR_API_KEY`, `GERADOR_MCP_LEAD_DATA`) e a chave, que
  não aparece em `repr`, em retorno de ferramenta nem em log;
- `catalog`: lê o `openapi.json` e gera uma ferramenta por operação (nome, descrição, esquema de
  entrada, dicas), filtrada pelo papel da chave e pela opção do dado da lead. Não depende do SDK;
- `client`: o cliente HTTP (httpx) e a montagem da requisição de cada ferramenta;
- `results`: o que volta (JSON, erro da API, arquivo salvo para o PDF e o HTML);
- `runner`: a chamada de uma ferramenta, com a confirmação antes das operações marcadas;
- `compose` e `composites*`: as cinco ferramentas compostas, escritas à mão, que só chamam ferramentas
  geradas pelo `runner` (a confirmação, o dado da lead e o papel da chave valem dentro delas);
- `server`: a cola com o SDK do MCP (o extra opcional `mcp`) e o transporte stdio.

Visão geral e configuração: `app/README.md`, "MCP de admin". Decisões: ADR 0015, "O caminho até o MCP"
e "Decisões da fase 9.1" e "da fase 9.2".
"""
