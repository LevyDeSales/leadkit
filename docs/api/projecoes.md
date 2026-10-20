# Planos por audiência e finalidade do receptor

No pedido administrativo, **Plano gerado · uso interno** identifica o documento integral.
**Ver como participante** abre outra página, construída com a projeção antes de serializar;
o documento interno não acompanha o HTML, nem em elementos ocultos.

`GET /admin/api/v1/jobs/{public_id}/plan-projection` (`get_job_plan_projection`) exige chave `total`.
`audience=participant` é o padrão; `internal` conserva o documento validado integral. A resposta
contém `public_id`, `form_slug`, `form_version`, `form_version_number`, `attempt`, `audience` e
`lead_data.document`, sem respostas, contato, consentimentos ou tracking. O MCP deriva a mesma operação e
só a oferece com papel `total` e `GERADOR_MCP_LEAD_DATA=1`. Ausência de plano ou classificação
histórica verificável retorna `409 state_conflict`, sem usar a definição atual como fallback.

Ao criar um destino, selecione `purpose=internal|participant`. Interna pode receber dados comerciais
não apresentados à participante; participante recebe só o plano público e referências/links já
permitidos. Omitir finalidade nunca cria uma autorização interna. Em atualização, omitir conserva
a escolha somente se a URL não mudar; mudar a URL exige finalidade explícita. Trocar o segredo
conserva a finalidade. `purpose_origin` identifica `legacy`, `explicit` ou `default`.

O webhook interno conserva o envelope existente de `plano.concluido`, incluindo plano integral,
contato, respostas, consentimento, tracking e classificação comercial. O envelope participante
contém somente `event`, `event_id`, `occurred_at`, `form`, `job`, `plan`, `pdf` e
`audience=participant`; `plan` é a projeção pública, `job` contém código, tentativa e links de
estado/página, e `pdf` mantém os links e referências técnicas sujeitos às permissões vigentes.
Nenhuma liberação comercial ou revogação retroativa de URL é introduzida neste corte.

Captura (`submissao.persistida`) e Baserow exigem finalidade interna. Escolha um receptor interno
autorizado ou remova a integração efetiva, conferindo também o destino padrão herdado. A mesma
regra vale em salvar, testar, ativar, publicar, importar e antes da rede, nas superfícies comuns.

Novas intenções PDF fixam o identificador do receptor, sua revisão, audiência e tentativa.
Mudar revisão bloqueia a repetição automática. Para repetir explicitamente uma entrega falha,
leia a revisão atual e informe `expected_config_version` e `expected_attempt`, confirmando a ação.
O receptor precisa ser o mesmo e a audiência não muda; sucesso e callbacks de tentativas de
dispatch antigas são inofensivos. Uma entrega de tentativa anterior ou com dados excluídos não
recria o pedido. Sem destino quando o PDF termina, não nasce intenção nem há envio retroativo.
Históricos sem correlação conservam apenas a resolução interna legada.

A finalidade participante depende da atestação operacional de todas as réplicas compatíveis;
sem ela, criação, reclassificação e despacho são recusados. API e worker conferem o piso
antes de preparar destinos; não iniciam se o banco não permite comprovar compatibilidade. Consulte o roteiro de
[operação Swarm](../../deploy/swarm/README.md) antes de atualizar ou tentar rollback.
Para um novo bootstrap por ambiente, `WEBHOOK_PURPOSE=internal` explicita a autorização interna;
`WEBHOOK_URL` e `WEBHOOK_SECRET` sozinhos não a concedem. Depois da primeira semeadura, o destino
continua sendo administrado no painel.
