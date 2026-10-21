# API de admin: tudo o que o painel faz, por chave com papel, com referência Scalar e um MCP derivado dela

Estado: adotado.

Admin, API e MCP compartilham serviços e regras de autorização. Chaves têm papéis de acesso e auditoria.

## Adendo: credencial de serviço para encaminhamento

A gestão de encaminhamento mantém os papéis administrativos `leitura`/`total`, os serviços
compartilhados e a confirmação nas superfícies HTML, API e MCP. O resolvedor vive na API separada
`/integrations/v1/handoffs/resolve`, com contrato próprio; não é operação administrativa nem
ferramenta MCP. A credencial `lki_` autentica somente um receptor, revisão, formulário, finalidade
`support` e projeção `correlation.v1`, com escopo exclusivo `handoffs:resolve`.

Chave administrativa, recibo e token de PDF não autorizam esse endpoint. A referência `lkh_`
correlaciona uma submissão; não identifica a participante, entrega respostas ou libera plano.
A autenticação antecede a interpretação do corpo e é revalidada na transação da projeção mínima.

A criação e a rotação exigem uma escolha explícita de vencimento: `expires_at: null` significa
sem vencimento. O segredo utilizável aparece somente na resposta inicial; o banco conserva seu
hash e metadados operacionais. A chave de idempotência vai no corpo, preservando a paridade MCP;
repetir uma emissão confirmada devolve conflito com identificador seguro, sem recuperar segredo.
A rotação revoga a anterior atomicamente e não transfere autorização entre revisões.
