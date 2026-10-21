# Central de integrações

A operadora abre **Integrações** no menu do admin para encontrar Baserow e Webhook por formulário,
mesmo sem conexão, em rascunho ou inativo. A conclusão da participante continua no formulário.
A central só lê o banco local; a validação do provedor permanece nos detalhes existentes.

`GET /admin/integrations` e `GET /admin/api/v1/integrations` compartilham a mesma consulta.
Na API e no MCP, a operação é `list_integrations`, com papel mínimo `leitura`, sem confirmação,
marca de efeito externo ou dado pessoal. A referência Scalar e o catálogo MCP vêm do OpenAPI.
Filtros: `connector=baserow|webhook`, `form_slug` opcional, `limit` de 1 a 200 (padrão 50) e `cursor`
opaco. Filtros e próxima página da tela funcionam por GET, sem JavaScript.

A resposta contém `catalog`, `items` e `next_cursor`. Os contadores de `catalog` são globais:
configurações, conexões ativas (apenas Baserow) e formulários com receptor. Configurações órfãs
contam como configurações, mas não como formulários existentes atendidos. O padrão conta uma vez.

Cada linha distingue `scope_kind=default|form` e `form_slug` anulável. Identidades:
`webhook:default`, `webhook:form:<slug>` e `baserow:form:<slug>`. Filtrar um formulário retorna
somente suas linhas. `configured` indica configuração própria; `serving=own|default|none`
identifica quem o serve. `receiver` descreve o receptor efetivo, inclusive escopo e revisão:
a revisão do padrão herdado **não** é a versão para criar um destino próprio. Leia o detalhe
antes de escrever; criação continua usando a versão `0` do recurso próprio.

`receiver.active` só existe como booleano no Baserow; no Webhook é nulo. Uma conexão ativa não
significa que esteja saudável. `mapping` indica presença dos perfis salvos, sem validar a tabela
atual. `provider_verification=not_checked` em toda linha. Pendências locais são contextuais ao
conector. Credencial ilegível não impede a leitura das outras configurações. Uma versão publicada
incompatível continua listada com perfil indisponível e aviso seguro. Baserow órfão não oferece
link para um detalhe que exige formulário existente.

`history.pdf` e `history.capture` contam intenções conservadas nos estados `pending`, `succeeded`
e `failed`. `last_success` é a maior confirmação de sucesso registrada para o receptor atual,
qualificada por `configuration_match=current|previous|unknown`. Capturas e Webhook PDF usam a
revisão registrada; Baserow PDF não possui revisão histórica e retorna `unknown`. Identidades
removidas/recriadas e PDFs legados sem receptor não herdam confirmação por host ou fallback.
Sem instante de sucesso, `last_success=null`: **Sem confirmação registrada**. Datas da API são UTC;
a página usa São Paulo. O resumo do padrão é compartilhado e não se soma às linhas herdadas.

Sucesso confirma transporte, sem provar leitura, atendimento ou liberação do PDF. Retenção e
reenvios podem alterar o histórico conservado. A leitura não expõe URL completa, token, segredo,
cifra, payload, resposta da participante ou erro bruto, mesmo para papel `total`.

Links da central apenas navegam: configurar, mapear, abrir teste, revisar ativação ou envios usam
os detalhes atuais. Teste Baserow grava uma linha sintética que permanece na tabela, após ação
explícita no detalhe. Webhook não tem ação de ativação. Escritas preservam autorização, CSRF,
confirmação e versão esperada existentes; nenhum comando novo foi criado.

Filtro de formulário desconhecido retorna `404 not_found`; conector, limite ou cursor inválidos,
`422 validation_failed`. API sem chave válida retorna `401`; limite de uso, `429` com `Retry-After`;
banco indisponível, `503 unavailable`, sem falsa lista vazia. A tela sem sessão redireciona ao login.
Veja a [matriz de cobertura](cobertura.md#central-de-integrações) e o [OpenAPI](openapi.json).
