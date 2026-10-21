# Eventos externos da jornada

No formulário, abra **Eventos externos**, crie uma fonte pausada e conceda somente os tipos, canais
e evidências que o adaptador pode declarar. Escolha a validade da credencial, guarde o segredo
mostrado uma vez e habilite explicitamente a fonte. Mudanças de política pausam a fonte e exigem
credencial da revisão nova. Nomes podem mudar sem transferir autoridade histórica.

A credencial exclusiva `lke_` autentica `POST /integrations/v1/journey/events`; chaves de admin,
resolução `lki_`, link `lkh_` e tokens PDF não substituem essa autoridade. O schema de integrações
fixa o corpo JSON fechado, até32KiB, um evento por chamada. `reference_id` é o UUID persistido
retornado pelo resolvedor de continuidade; nunca procure uma participante por nome, contato ou
ID externo para selecionar diagnóstico.

O corpo contém `schema_version:1`, `source_revision`, `event_id`, `reference_id`, `type`,
`entity:{kind,id,revision}`, `occurred_at` com offset, `evidence:{channel,kind,id,author:{kind,id}}`
e o snapshot completo `data` do tipo. Campos nullable precisam estar presentes. Todos os IDs
externos são opacos e sensíveis; não envie nomes, contatos, mensagens, URLs ou segredos. O core
limita o formato e protege as projeções, mas não detecta toda PII disfarçada de ID: o adaptador
precisa comprovar sua adequação.

| Tipo | Entidade | Evidência |
| --- | --- | --- |
| correlation.linked | context | correlation_record |
| invitation.sent | invitation | dispatch_record |
| meeting.state_reported | meeting | calendar_record |
| meeting.attendance_recorded | attendance | attendance_record |
| notification.delivery_reported | notification | dispatch_record para accepted/failed; provider_delivery_receipt para confirmed |
| participant.receipt_acknowledged | receipt | participant_ack, autora participant |
| entity.withdrawn | uma entidade acima | operator_correction e autoridade do tipo-base |

A maior revisão da entidade prevalece por fonte e diagnóstico. Revisões menores inéditas ficam
no histórico; repetir `event_id` e conteúdo canônico devolve o recibo original. Conteúdo divergente
retorna409; outro ID para a mesma revisão também conflita. Retirada não restaura estado antigo.
Comparecimento aponta `meeting_id` e `meeting_revision` da mesma fonte. Remarcar não transporta
presença; agenda ausente continua ausente. Datas exigem offset e zona IANA válida. O horário da
reunião pode ser futuro; ocorrência tolera até cinco minutos de diferença à frente do servidor.

A resposta201 é persistência nova;200 é repetição exata. Nenhuma confirma leitura humana, libera
PDF, agenda reunião ou envia mensagem. Auth inválida/pausada responde401 antes do JSON; política
não concedida403; correlação ausente, alheia, revogada ou purgada404; conflitos409; tamanho413;
formato415; validação422; limite local429 com Retry-After; manutenção/banco503. Os limites são
locais ao processo:60 chamadas/minuto por credencial,10 recusas/minuto por IP. Erros nunca ecoam
corpo ou IDs externos. Respostas privadas usam no-store/no-referrer/noindex.

A jornada parte da captura ou pedido e separa fatos locais, autorização do plano, transporte e
relatos externos. API e MCP oferecem a mesma gestão e consulta em `/admin/api/v1`; a leitura
padrão não mostra identificadores externos. `include_lead_data=true` exige papel total e, no MCP,
`GERADOR_MCP_LEAD_DATA=1`. A tela oferece opt-in explícito e filtros GET sem JavaScript. A agenda
mostra a zona registrada; ocorrência e recebimento usam Brasília no painel e UTC na API.

Apagar os dados externos também encerra o encaminhamento individual, atomicamente, com
`expected_journey_version`, `expected_handoff_version` e confirmação. Não apaga respostas ou PDF,
nem cópias na fonte. Tombstones mínimos somem na retenção original. O vencimento apenas do link
bearer não impede eventos enquanto a submissão e a autorização histórica forem conservadas;
revogação, pausa ou purga impedem retransmissão e não recriam vínculo.

A migration0018 acrescenta três tabelas sobre0017, conserva o piso2 e não semeia fontes ou
credenciais. Instale com migrador que conhece0018; migrador2 anterior recusa o head novo. O retorno
a um runtime2 anterior depende da prova de compatibilidade e não oferece endpoints de eventos.
A prova sintética de código não valida emissor real, ativação comercial ou conformidade universal.
