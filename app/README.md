# Referência técnica — Leadkit

O motor recebe um formulário em `/p/{slug}` ou `POST /v1/forms/{slug}/submissions`, valida as
respostas contra a versão publicada e cria um pedido idempotente. Tasks Hatchet geram o plano,
validam o documento, renderizam o PDF e entregam `plano.concluido` a um webhook assinado.
O reconciliador recupera trabalhos pendentes e a retenção remove dados vencidos.

## Configuração

Copie [app/.env.example](.env.example) para `.env` e use valores próprios. As opções e validações
estão em [settings.py](settings.py): `DATABASE_URL`, `PUBLIC_BASE_URL`, `TOKEN_SECRETS`, Hatchet,
retenção, LLM, PDF, storage e admin. `DATABASE_URL` precisa também ser exportada no shell para
Alembic; ele não carrega `.env`.

`LLM_MODE=fake` usa o plano de exemplo da versão; `PDF_MODE=weasyprint` gera PDF real;
`STORAGE_MODE=local` grava em `LOCAL_STORAGE_DIR`. O worker ainda precisa de Hatchet. Para uso com
provedores, configure OpenAI e S3 com credenciais da sua instalação. Não coloque contatos da lead
nos inputs/outputs ou logs das tasks.

## Administradora

Defina juntas `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` (Argon2) e `ADMIN_SESSION_SECRET` (32 ou mais
caracteres aleatórios). Gere o hash sem colocar a senha no histórico do shell com
`python -c 'from argon2 import PasswordHasher; from getpass import getpass; print(PasswordHasher().hash(getpass()))'` e guarde os segredos no
ambiente. O admin fica em `/admin`; suas páginas permitem importar o demo, editar o rascunho,
executar teste e publicar. Uma publicação fixa a definição e as imagens usadas por ela.

`WEBHOOK_ENCRYPTION_KEY` deve ser uma chave Fernet válida, gerada e guardada pela operadora:
`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.
Mantenha a chave em backups seguros: trocá-la sem o procedimento de rotação impede ler conexões.

## Formulários e templates

O exemplo completo está em [examples/demo.json](../examples/demo.json); o starter do admin fica
em [forms/starter](forms/starter/definition.json). O compilador documenta o formato 1 em
[forms/definition](forms/definition/__init__.py). Ele valida perguntas, condições, mapeamento de
identidade, consentimentos, dados, pontuação, calendário e modelo do plano.

O prompt recebe `answers`, `labels`, `scores`, `today`, `data` e `phases`. PDF e prévia recebem
`plan`, `lead.name`, `form.title`, `form.slug`, `scores` liberados, `generated_date` e macros `brand`.
Contato não deve ser interpolado no prompt. Imagens do formulário usam `asset:nome` e ficam fixadas
por conteúdo na versão. O PDF aceita recursos locais confinados, `data:` e imagens do formulário;
URLs de rede são recusadas. Veja [marca privada](../docs/branding.md).

### Conclusão após salvar respostas

O bloco opcional `completion` pertence à versão publicada. Sem ele, a participante acompanha o
resultado do PDF como antes (`mode: result`). Os outros modos são `confirmation` (recibo local),
`button` (recibo com botão) e `redirect` (recibo com encaminhamento automático e botão de continuidade).
`message` é texto puro de 1 a 2000 caracteres, com padrão “Recebemos suas respostas. Obrigada por
participar.”; `button_label` tem de 1 a 300 caracteres e padrão “Continuar”. HTML é escapado e texto
não é interpretado como template. O botão de envio do formulário mantém o texto legado.

`url` é obrigatória em `button`/`redirect` e não se aplica aos modos locais. A configuração
administrativa autoriza a URL HTTPS exata, sem usuário/senha ou override público; o produto não
acrescenta respostas, dados pessoais ou credenciais a ela. Não há lista global adicional de domínios.
Sem JavaScript, a participante usa o botão do recibo. O botão continua disponível ao voltar à página.

Editor, API e MCP editam o mesmo documento. A prévia “Conferir conclusão salva” usa o rascunho,
mostra o destino como texto e desativa a navegação; também está disponível na operação
`render_form_draft_completion_preview`. Mudar somente conclusão não invalida o teste do LLM.
Importação, cópia e exportação conservam a configuração pela versão.

A confirmação só acontece após commit, e reenvios conservam a versão original, mesmo após nova
publicação. O disparo de geração inicia sem prender a resposta; falha ou interrupção conserva o job
persistido para o reconciliador. A API pública mantém os campos do pedido e acrescenta `completion`,
incluindo `receipt_url` local. O token desse recibo é trocado por cookie como no resultado existente.
Não envie esse token ao destino externo. A conclusão não bloqueia nem libera PDF: a geração e os
acessos atuais continuam funcionando. Esta opção ainda opera apenas com geração de PDF.

O runtime atualizado lê versões antigas. Uma versão publicada com `completion` exige um
compilador que conheça esse bloco: depois dessa publicação, trocar apenas a imagem por uma anterior
à funcionalidade não garante um rollback de conteúdo seguro. Preserve os dados e escolha um runtime
compatível com as versões publicadas; não altere uma versão publicada para tentar voltar a imagem.

## API e MCP

A especificação está em [docs/api/openapi.json](../docs/api/openapi.json); a referência interativa
fica em `/admin/api`. Chaves de API têm papéis próprios e não substituem a sessão do admin.
A página `/admin/mcp` fornece a configuração do cliente e as instruções para criar uma chave.
O servidor pode ser executado com `python -m app.admin_mcp`; preserve o nome das ferramentas e
os contratos HTTP ao integrar clientes existentes.

O venv de uma instalação editável do MCP aponta para o clone. Mantenha a pasta no mesmo caminho e
na versão escolhida; mover ou apagar a pasta quebra a instalação. O campo de Python aceita um venv
em outro disco. As datas de validade da chave na página usam o horário de Brasília.

### Confirmação e recuperação do MCP

O formulário de confirmação exige `accept` com `confirmar` booleano `true`. Caixa desmarcada,
recusa (`decline`), cancelamento (`cancel`), resposta incompleta/inválida, indisponibilidade e
timeout bloqueiam a operação e apagam todas as confirmações pendentes da mesma chamada, inclusive
as adicionais. `confirm: true` nos argumentos não contorna esses resultados. A mensagem descreve
o que o cliente informou; não comprova que uma janela apareceu ou que a operadora interagiu.

Somente cliente inicializado sem suporte declarado a formulário recebe o fluxo de duas etapas:
primeiro a descrição, depois a chamada idêntica com confirmação explícita. `elicitation: {}`
continua compatível com formulário; suporte apenas a URL não o substitui. Sessão não inicializada
e falha técnica não habilitam duas etapas. A pendência vale por 15 minutos e para uma execução,
vinculada a operação, argumentos, revisão, contexto composto e bytes do arquivo. Com formulário,
as confirmações adicionais são pedidas de novo e não herdam flags de um fallback anterior.

A chamada fica reservada antes da espera assíncrona, inclusive durante confirmações adicionais.
Uma repetição idêntica concorrente informa que já há tentativa em andamento: aguarde o resultado e
confira o estado antes de repetir. Erro ou cancelamento libera a reserva local e invalida a
confirmação, inclusive quando a pergunta própria de uma composta é interrompida; não autoriza retry
automático. Respostas HTTP incertas (como 500 ou 502 sem `upstream_failed`) também invalidam a
pendência inteira e exigem conferir o estado antes de uma nova apresentação e confirmação.
Recusas conhecidas sem efeito (4xx e `502 upstream_failed`, incluindo `409 version_conflict` e
`422 validation_failed`) mantêm a recuperação por nova chamada explicitamente confirmada.
A reserva pertence à sessão no processo MCP; não é um lock distribuído nem garante execução
exatamente uma vez na API remota. Leituras, simulações e argumentos diferentes seguem independentes.

Após falha de confirmação, confira a conexão do cliente e faça uma nova tentativa explícita.
Se a falha ocorreu **no envio à API**, confira o estado antes de repetir: a escrita pode ter
acontecido. Essa distinção também aparece nas ferramentas compostas.

O log `mcp_confirmation` vai a stderr e usa uma allowlist: identificador efêmero de tentativa,
desfecho, ação categorizada, tipos/presença dos campos, suporte anunciado, categoria de falha e
versões observáveis. `protocol_requested` é a versão oferecida pelo cliente; `protocol_negotiated`
fica `unknown`, pois o SDK atual não expõe esse resultado na sessão. `client_reported_version`
é apenas a versão numérica informada pelo cliente; `sdk_package_version` e `server_package_version`
identificam distribuições instaladas, sem afirmar o build do processo. Valores não reconhecidos
ficam `unknown`. Não registra perguntas, argumentos, caminhos, conteúdo das respostas,
`clientInfo` bruto, texto de exceções ou segredos. O servidor não observa a renderização da UI;
diagnosticar uma janela ausente exige evidência própria do cliente efetivamente usado.

## Webhook

O corpo JSON é assinado com HMAC-SHA256; os nomes dos headers `X-MCC-Event`, `X-MCC-Timestamp` e
`X-MCC-Signature` são contratos de compatibilidade. Confira timestamp e assinatura antes de
processar o evento; repetições devem ser idempotentes. Não registre o corpo em logs públicos.

## Deploy e retenção

Os exemplos [Compose](../deploy/compose.yaml) e [Swarm](../deploy/swarm/README.md) precisam de
configuração própria. Migre antes de iniciar o serviço e use uma versão fixada. Teste backup e
restauração em ambiente isolado antes de atualizar. Não use a baseline de banco vazio sobre uma
instalação existente sem um plano de compatibilidade aprovado.

`LINK_TTL_DAYS`, `RETENTION_PDF_DAYS` e `RETENTION_DAYS` controlam validade e limpeza; formulários
podem definir prazos próprios compatíveis. A exclusão lógica do link e a remoção física do PDF
são etapas distintas. Quem opera o sistema é responsável por consentimento, retenção e fornecedores.

O [roteiro de migração](../docs/migrations.md) detalha o guard, os ensaios e a preservação de
fonte, código, dados, storage, marca e chaves na futura adoção do núcleo.

## Disponibilidade do PDF da tentativa atual

O processamento (`status`/`stage`) é separado de `current_pdf` e da autorização. `completed`
continua sendo o registro da geração, mesmo se o arquivo tiver sido removido. A consulta autenticada
`GET /v1/jobs/{public_id}` e `get_job` no admin/API/MCP usam o mesmo serviço, com a tentativa atual,
existência no storage, validade técnica, acessibilidade e instante da verificação dos bytes.
A consulta de estado lê no máximo um byte do arquivo e não inicia uma geração. A lista de pedidos conserva
seu contrato e não consulta storage; abra o detalhe para conferir a disponibilidade atual.

| `current_pdf.state` | Significado |
| --- | --- |
| `available` | Tentativa concluída, bytes verificados e objeto atual acessível com a mesma identidade e tamanho. |
| `missing` | A tentativa atual não tem artefato ou o storage confirmou ausência. |
| `removed` | Retenção registrada; o download responde 410. |
| `unavailable` | Não foi possível conferir o acesso, inclusive timeout e permissão negada; não afirma inexistência. |
| `unknown` | Sem prova suficiente, inclusive histórico anterior à migration 0013; não anuncia pronto. |
| `invalid` | Objeto mudou de identidade ou tamanho em relação à prova persistida; não é servido. |

`exists`, `valid` e `accessible` podem ser nulos quando não há evidência. `available` é uma
conveniência técnica e nunca concede acesso: token válido continua obrigatório na interface pública;
o download administrativo exige sessão de operadora ou chave `total`. No MCP, o download mantém a
exigência da opção de dados da lead. A consulta de disponibilidade administrativa exige `leitura`
e não traz documento, token, chave de storage, identidade opaca do objeto ou hash do conteúdo.
A política adicional de liberação por tentativa é tratada na #13.

Na conclusão, o serviço confere o vínculo pedido/tentativa/plano, lê o objeto, verifica hash SHA-256
e tamanho, compara a identidade do storage antes/depois e abre o PDF em modo estrito com pypdf.
Exige documento não criptografado, ao menos uma página com dimensões positivas e streams legíveis;
streams de conteúdo FlateDecode passam por zlib sem recuperação permissiva, inclusive em arrays
e cadeias de filtros, com limite de descompressão. Não altera o parser global nem usa qpdf em runtime;
cabeçalho `%PDF` isolado é insuficiente. Isso não avalia aparência, completude do texto, segurança de
conteúdo ativo nem qualidade semântica. pypdf já estava no lock sob BSD-3-Clause e passa a ser
uma dependência de runtime; a versão e os avisos seguem em `THIRD_PARTY_NOTICES.md`.

Consultas posteriores conferem metadados e provam acesso ao conteúdo com leitura de um byte:
GET com `Range: bytes=0-0` no S3 e leitura limitada com identidade do descritor no storage local.
O stream sempre é fechado; a identidade/tamanho observados devem coincidir com a prova persistida.
HEAD isolado não prova leitura, inclusive com SSE-KMS. Provedores sem prova limitada retornam
`unknown`, sem fallback para download integral. ETag e VersionId são identidades opacas, não hashes
SHA-256. Essa observação não garante disponibilidade
futura de um link: storage e retenção podem mudar depois da consulta. Downloads leem e conferem
novamente os bytes antes de responder ou assinar a URL. Reprocessamento e gravação/conclusão são
serializados por pedido; uma renderização duplicada não sobrescreve o arquivo da vencedora.
O download público relê a tentativa e a versão do token sob esse bloqueio.

Históricos não recebem prova por backfill. A ação **Verificar e baixar PDF** em uma consulta
autorizada valida o arquivo real e registra a evidência sob bloqueio, sem mudar o estado histórico,
sem inventar eventos do engine e sem usar IA. Um histórico inválido ou inacessível falha com erro
seguro. Essa escrita técnica também acontece no download API/MCP autorizado; nenhuma consulta de
status a provoca. Sem `version`, o download administrativo seleciona exclusivamente a tentativa
atual; a operadora pode pedir uma versão histórica explicitamente.

Os links de PDF em eventos de conclusão e no mapeamento `job.pdf_url` fixam a tentativa observada
em `attempt`. O endpoint público recusa com 409 uma tentativa que já não é atual; o link antigo não
passa a baixar outros bytes com outro hash. Uma entrega de webhook ainda pendente de tentativa
anterior é recusada com código seguro, sem enviar conteúdo antigo como atual. O mapeamento Baserow
continua sendo por pedido/submissão, sem documento/hash de plano; apenas seu link fixa a tentativa
observada na montagem da linha. Isso não cria controle de ordem de eventos nem altera o upsert.
URLs diretas de storage já emitidas conservam a validade original; esta mudança não revoga URLs
pré-assinadas anteriores nem cópias já baixadas.


## Integrações de captura

Formulários de captura salvam respostas sem IA ou PDF. Na mesma transação, cada conexão Baserow
ativa e o webhook efetivo recebem uma intenção própria; sem configuração, nenhum destino é
inventado. Replay conserva as intenções originais e ativar uma conexão depois não faz backfill.

Configure o perfil **Captura sem PDF** no Baserow (`submission.id` como chave); o perfil PDF
histórico usa `job.public_id`. A conexão tem uma ativação, aplicável à finalidade publicada.
O webhook precisa declarar `submissao.persistida` em **Eventos aceitos pelo receptor**; defaults
preservam apenas `plano.concluido`. A publicação, importação e simulação apontam incompatibilidades
localmente, sem consultar o provedor. Testes de configuração usam dados sintéticos marcados como
teste e exigem a confirmação externa, sem criar uma submissão verdadeira.

O despacho ocorre depois do commit no workflow `deliver_capture`, com entrada/saída somente de
referências e contagens. Falhas do SDK ou provedor não prendem a participante. O reconciliador
recupera intenção sem run e run perdido usando novo epoch, sem aceitar callback de dispatch antigo.
O evento conserva seu ID em retries: receptores devem deduplicar por `event_id`; Baserow faz upsert
pela submissão. Timeout pode repetir uma requisição, sem promessa de entrega exatamente uma vez.

Consulte entregas na captura ou integração. **Repetir entrega** exige confirmação, epoch e revisão
atual do mesmo destino; mudança de configuração interrompe o envio e não redireciona dados
silenciosamente. Sucesso comprova transporte HTTP/gravação na planilha, sem provar leitura pela
participante. Estado não inclui respostas ou erro bruto; dados pessoais continuam exigindo papel
total e opt-in na API/MCP. Retenção conta da submissão original, trava com o despacho limitado e
apaga intenções junto da captura. Tasks tardias não enviam nem recriam dados excluídos. Dados já
recebidos pelo provedor seguem a retenção operada fora do Leadkit.

### Audiência e finalidade dos receptores

A consulta administrativa [por audiência](../docs/api/projecoes.md) conserva o plano integral para
uso interno e oferece **Ver como participante** sem serializar conteúdo interno oculto.
Destinos novos declaram finalidade; captura e Baserow exigem `internal`. Um novo bootstrap do
webhook por ambiente exige `WEBHOOK_PURPOSE=internal` para autorizar o envelope interno.
Destinos existentes conservam a origem legada; girar o segredo mantém a finalidade, mudar a URL
exige escolha explícita. Novas entregas PDF fixam receptor/revisão/audiência/tentativa e o reenvio
exige confirmar as versões atuais. Habilitação participante e rollback dependem do roteiro de
compatibilidade operacional, sem alterar o guard estrutural do banco.

### Liberação individual do PDF

Formulários PDF podem optar por `release: {mode: "manual"}` em uma nova versão. Ausência conserva
a entrega automática. Capturas recusam esse bloco. Geração concluída e PDF disponível são estados
separados do acesso: no manual a participante vê “Seu plano aguarda liberação”, sem prévia,
conteúdo oculto ou URL de download. A política vem da publicação respondida; histórico irresolvível
protege o acesso até investigação administrativa.

A responsável libera a tentativa observada pelo admin, `release_job_pdf` na API de admin ou a
mesma ferramenta MCP, com papel total, confirmação, versão e idempotência. Pode escolher outra
jornada, sem compra ou reunião. Uma ação aceita grava fato e intenção de notificar na mesma
transação, sem renovar retenção. Sem canal participante a resposta informa “acesso liberado;
notificação não enviada”. Reprocessar mantém a nova tentativa retida; replay do comando anterior
retorna o fato anterior, sem nova geração ou notificação.

`plano.liberado` exige opt-in no destino participante. No PDF manual, o receptor pode selecionar
conclusão, liberação ou ambos; conclusão só é criada quando selecionada e fica bloqueada até a
liberação. Timeout de transporte significa resultado não confirmado, com o mesmo event_id no retry.

Receptores internos usam links próprios `/i/jobs/…`, com capacidade `i1` revogável por receptor,
revisão e tentativa. Página/status trocam o segredo de query por cookie HttpOnly de escopo restrito;
PDF pode consumir a query diretamente. O token público nunca promove a audiência. O PDF continua
sendo a projeção participante existente; o documento integral só segue pelas fronteiras internas
já autorizadas. Baserow PDF mantém os locks de configuração, pedido e artefato atuais até o upsert
limitado; a captura conserva seu fluxo próprio.

URLs S3 já emitidas antes desta atualização podem continuar válidas por até24 horas. As novas
emissões respeitam o prazo do pedido; revogação não retira URLs ou cópias já recebidas. A operadora
continua responsável pela limpeza de dados nos receptores. O proxy precisa omitir query, headers
Authorization e cookies das rotas de capacidade, e corpos do resolvedor de encaminhamento dos logs;
o filtro Python de access log não configura nem comprova o proxy externo.

## Jornada externa de atendimento

A jornada de cada submissão, inclusive captura sem PDF, separa fatos locais de declarações externas.
Fontes nascem pausadas, com credenciais próprias e autoridade histórica explícita. O painel em
Eventos externos e Jornada usa os mesmos serviços da API de admin/MCP; ingestão é uma API de serviço
separada. Veja [contrato, autoridade e retenção](../docs/api/jornada.md). Eventos não executam
liberação, geração, agenda ou mensagens; integração real exige prova própria do adaptador.
# Envio de respostas persistidas

O webhook interno opt-in `submissao.persistida.v2` reutiliza a outbox `capture_deliveries` em
captura e PDF. O envelope2 e o emissor da instalação ficam fixados no banco no aceite completo;
Hatchet recebe só IDs/contagens. A cópia das respostas na outbox é removida pela cascata da
submissão e não entra em consultas administrativas de estado. Veja o [contrato](../docs/api/integracoes.md)
e a [adoção do piso3](../deploy/swarm/README.md). A opção fica desligada por padrão.

### Comando idempotente do teste de rascunho

Antes de `start_form_draft_test`, `get_form_draft_test_command_context(slug)` fornece `command_id`,
`expected_version`, relógio e limite da criação, sem efeito persistido. Conserve o ID antes do POST.
Envie-o com a revisão e a confirmação atual; respostas/data omitidas são fixadas na primeira criação.
O resultado identificado é `receipt`, com IDs seguros, principal da chave, revisão, instantes,
`dispatch_state`, `test_status` e `result_state`. O legado sem command_id conserva sua resposta anterior.

MCP faz preflight com o mesmo corpo e `replay_only:true`, dispensado de confirmação pelo contrato
existente. Esse modo é leitura mesmo quando o comando não existe. Somente `404 command_not_found`
permite perguntar e tentar a primeira criação; `403 command_forbidden`, `409 command_conflict`,
`410 command_expired`, `422 command_in_future` e falhas de transporte não seguem para efeito.
Consulta direta usa `get_form_draft_test_command(slug, command_id)`. A identidade não autentica e
recibos de outra chave/admin não são transferidos por paridade.

A janela v1 fixa é de três dias para criar desde emissão e três dias para consultar desde commit;
timestamps futuros são recusados. Os prazos não renovam aprovação. Após descarte, identidade antiga
nunca vira novo teste, mesmo com mudança de retenção dos resultados. `uncertain` indica ausência
de confirmação do despacho, sem afirmar custo zero; consulta/replay jamais reenfileiram. Para nova
execução, obtenha outra identidade e outra confirmação. Native decline/cancel/falso/inválido/falha
continuam bloqueando, e a compatibilidade unsupported não comprova clique humano.
