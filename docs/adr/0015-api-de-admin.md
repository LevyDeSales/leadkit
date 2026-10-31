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

## Autenticação e projeção de eventos externos

A gestão de fontes, credenciais e jornada integra integralmente admin/API/MCP e usa os mesmos
serviços, confirmação e versões esperadas. A ingestão é outro serviço, documentado no OpenAPI de
integrações em `POST /integrations/v1/journey/events`, fora do catálogo administrativo. A credencial
`EventCredentialAuth`, prefixo `lke_`, concede exclusivamente `journey:events:write` à revisão atual
da política de uma fonte; não resolve encaminhamento, administra recursos ou abre PDF. Mantemos
as constraints de `IntegrationCredential` intactas, evitando transferir autoridade de resolução
para declarações comerciais.

A autenticação antecede o corpo JSON limitado a32KiB. Fontes nascem pausadas e fixam formulário,
receptora, revisão histórica e namespace. A política explicita tipos, canais e evidências; mudar
permissões cria revisão, pausa e invalida credenciais antigas. Um link de continuidade vencido não
encerra a referência persistida enquanto a submissão estiver retida, mas revogação individual,
pausa da fonte e revogação/vencimento de credencial impedem novos eventos. Rotação não muda a fonte.

Agenda e comparecimento têm entidades e revisões independentes. A presença indica a revisão exata
da reunião da mesma fonte; lacunas não inventam agenda nem reaproveitam presença de outra revisão.
Snapshots completos permitem chegada fora de ordem, sem regredir para uma revisão menor ou
ressuscitar o estado anterior a uma retirada. Nenhuma declaração executa liberação, geração ou
transporte. A aceitação comprova persistência/autenticação, não a veracidade comercial.

Todos os identificadores externos, autoria e evidências são tratados como dados da participante,
expostos exclusivamente em `lead_data` com papel total e opt-in, acrescido da flag do MCP. O formato
de IDs limita o contrato, mas não detecta universalmente nome, contato ou segredo disfarçado; a
adequação dos IDs e evidências exige prova do adaptador. Consultas padrão carregam somente os campos
seguros; o JSON protegido é diferido e não participa da serialização padrão.

Apagar dados externos exige confirmação e versões de jornada/encaminhamento, remove o JSON protegido
e os estados externos e revoga a referência na mesma transação. Permanecem tombstones técnicos até
a retenção da submissão, sem prolongar prazos, apagar respostas ou revogar PDF. O efeito não alcança
cópias remotas. Replay de apagamento já aplicado não registra nova auditoria. A FK de eventos para
a referência com CASCADE permite que a retenção anterior continue apagando filhos, sem adicionar
locks de Job/Lead à ingestão. A migration0018 é aditiva sob runtime2; o migrador anterior continua
recusando o head desconhecido, distinto da compatibilidade do runtime.

## Comando idempotente de teste de rascunho

O fluxo opt-in de `start_form_draft_test` usa identidade dt1 emitida sem persistência pelo
contexto específico do teste. Tipo/versão, namespace persistido existente, formulário UUID,
revisão obrigatória, entrada canônica e principal técnico (`api_key:key_id`, distinto do admin)
ficam vinculados a teste e auditoria na mesma transação. Somente a vencedora despacha após commit.
Identidade não é segredo, credencial, assinatura ou comprovação de pessoa autenticada.

`replay_only:true` na mesma operação é somente leitura via `x-confirm-unless`: compara argumentos
históricos e nunca cria ou reenfileira, inclusive em miss. MCP consulta antes de elicitation e
só `command_not_found` elegível permite seguir para a confirmação atual; autoridade incompatível
recebe erro opaco distinto. Negativas e a compatibilidade unsupported das #61/#63 permanecem.
O caminho legado sem identidade conserva contrato e revisão opcional, sem promessa de replay durável.

A janela dt1 é fixa: criação antes de emissão + três dias, timestamp futuro recusado e relógio
revalidado após locks; recibo mínimo antes de commit + três dias, sem renovação. Nenhum GET cria
namespace ou autorização. Despacho incerto não prova ausência de custo; replay não dá direito a
reenvio. Recibo mostra fatos recebidos da cliente, persistência e execução como estados separados.
