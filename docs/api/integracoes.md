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

A [jornada externa](jornada.md) recebe declarações autenticadas em um contrato separado de
resolução de encaminhamento e da administração. Sua fonte e credencial não autorizam transportes,
liberação de plano ou acesso às respostas. O esquema de integrações documenta ambos os serviços.
# Respostas após salvar: contrato opt-in

Em `save_destination`, configure o destino próprio de um formulário com `purpose: "internal"`
e `events: ["submissao.persistida.v2"]`, `confirm: true` e `expected_version` observado.
O seletor é exclusivo com `submissao.persistida`; o default PDF e o envelope legado não mudam
quando a opção está ausente. PDF pode selecionar somente esse evento ou selecionar também
`plano.concluido` explicitamente. Baserow conserva seus perfis existentes.

O seletor de configuração não muda o evento no transporte: `X-MCC-Event` e `event` continuam
`submissao.persistida`. O envelope tem `schema_version: 2`, `event_id`, `issuer`, `occurred_at`,
`form` (slug, versão textual e numérica), `submission` (id e submitted_at), `generation`,
`delivery_authorization` (destination_id, revision, purpose interna), `answers` e `consent`.
`correlation`, quando presente, contém somente reference_id, receiver_id e receiver_revision
históricos; não concede acesso nem amplia `correlation.v1`.

`issuer` é `leadkit:<UUID>` persistido uma vez no banco, conservado em backup/restore.
O diagnóstico é identificado por `(issuer, submission.id)`; o transporte, por `(issuer, event_id)`.
Nome/e-mail não são chaves. Os instantes com timezone representam o registro da submissão,
não o clique ou COMMIT literal. Consentimento é o JSON original: registros antigos não ganham
método/origem inventados. Não há link, token ou estado atual do PDF neste envelope.

O snapshot é fixado com submissão, Job/recibo e encaminhamento na mesma transação. O transporte
reutiliza HMAC-SHA256 sobre timestamp e bytes originais, `X-MCC-Signature: v1=<hex>` e
`Idempotency-Key: <event_id>`. Retry conserva ID e bytes; timestamp/assinatura de transporte podem
mudar. O receptor deve autenticar antes de gravar, deduplicar duravelmente por emissor/evento,
conflitar o mesmo ID com conteúdo divergente e responder2xx somente após commit do recibo/efeito.
Timeout depois do aceite pode causar outra requisição: a garantia demonstrável é um efeito lógico
deduplicado, não uma única execução de rede. A prova sintética local não afirma efeito Alltius.

Snapshots têm a mesma cascata de exclusão da submissão, sem prazo próprio, e não são carregados
nas consultas de estado, logs ou Hatchet. Pausa, mudança de revisão, remoção e retenção não
redirecionam a intenção nem recriam dados. Remover o destino apaga seu histórico local de entregas
por cascata, com contagens apresentadas antes da confirmação; respostas permanecem até sua retenção.
O teste do destino pode enviar envelope sintético da seleção atual, marcado `test:true`, sem criar
submissão real. Adoção exige piso3 e [roteiro de compatibilidade](../../deploy/swarm/README.md).
