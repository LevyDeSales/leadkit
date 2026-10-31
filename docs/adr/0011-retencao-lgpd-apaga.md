# A retenção LGPD apaga os dados, em vez de anonimizar

Estado: adotado.

Retenção distingue validade de link, remoção física de PDF e exclusão do pedido, com prazos compatíveis.


O resultado do teste de rascunho segue sua política própria: cinco recentes e três dias desde
início, com respostas, prompt, plano e PDF removidos juntos. O comando dt1 guarda somente vínculo
mínimo e fingerprints privados, nunca conteúdo, por três dias desde commit. Sua limpeza remove
somente o vínculo técnico e não antecipa nem prolonga a retenção do resultado. Enquanto retido,
um resultado já removido é explícito no recibo; depois, o timestamp integrado permite rejeitar a
identidade expirada sem tombstone eterno. A política dt1 é constante versionada independente de
TEST_DAYS: mudança futura de prazo não reabre identidades antigas. Auditoria existente não é
usada como registro alternativo para recriar comandos e não recebe conteúdo ou digests de respostas.
