# Projeção histórica e finalidade explícita dos receptores

O documento integral permanece interno; sua representação para a participante é derivada antes de
serializar, pela classificação da versão histórica que gerou o plano. PDF e prévia usam a mesma
projeção, sem alterar autoria, conteúdo publicado ou documento persistido. A projeção não concede
acesso público nem implementa a liberação por tentativa.

Cada receptor declara finalidade interna ou participante. Somente linhas anteriores à migração
conservam a origem interna legada; criar ou trocar a URL nunca autoriza uso interno por omissão.
Captura e Baserow recebem respostas e dados operacionais, por isso exigem finalidade interna e
recusam participante até existir um contrato próprio, sem inventar um envelope vazio.

Novas entregas PDF fixam receptor, revisão, audiência e tentativa na intenção transacional.
Repetição explícita pode autorizar uma revisão nova do mesmo receptor, conservando audiência e
tentativa; fatos históricos sem correlação permanecem internos legados. Isso troca a conveniência
do redirecionamento automático pela rastreabilidade da autorização. O transporte serializa com
mudanças de configuração e retenção; callbacks obsoletos não alteram outra repetição nem sucesso.

A primeira adoção exige interromper e drenar escritoras antigas antes da migração; o piso
operacional persistido e a atestação da frota compatível controlam a habilitação participante.
Uma flag ignorada por binários antigos não protege a configuração persistida. O roteiro de
compatibilidade deve impedir retorno abaixo do piso, inclusive rollback automático.

## Liberação e continuidade compostas

A capacidade2 reúne continuidade de atendimento e liberação individual. A migration composta
`0017_continuation_release` acrescenta as relações sem reescrever versões, credenciais ou tokens
históricos. Na mesma transação PostgreSQL fixa piso mínimo2, fase bloqueada e limpa epoch/evidência
anteriores. Este commit é o ponto após o qual imagens1 deixam de ser um retorno permitido.

A adoção reutiliza a pausa/drenagem existente: o helper descobre capacidade e piso antes de
mutações, fecha o gate, comprova a saída das escritoras antigas e persiste Current/Previous em zero
antes do DDL. Depois instala e normaliza ambas as configurações2, inicia novo epoch e atesta a frota.
Atestar não eleva o piso. Uma atualização2→2 com retorno compatível mantém o fluxo rolling.
Não há requisito nem prova de zero downtime para1→2; a pausa reduz a necessidade de convivência
entre parsers incompatíveis. O startup guard não encerra processos já vivos.

A liberação manual é opt-in na versão respondida e acrescenta autorização participante por
tentativa, além da projeção e da autorização histórica do receptor. O fato é independente de
`Job.updated_at` e dos prazos de retenção. Replay exato devolve o fato histórico antes de conferir
estado ou storage atuais, sem liberar nova tentativa nem despachar novamente. Sem canal participante,
a liberação registra a decisão de não notificar; nenhum receptor interno vira fallback.

O evento `plano.liberado` usa a outbox existente pelo canal `webhook.release`. Essa identidade
preserva a unicidade legada e permite coexistência com conclusão; o evento é selecionado
explicitamente. Todos os consumidores aplicam a mesma elegibilidade antes de disparar, transportar,
repetir ou tratar callback. A conclusão participante retida permanece uma intenção bloqueada.

Links internos usam domínio assinado próprio `i1`, sem segundo PDF integral. A aplicação confere
receptor/revisão/finalidade, tentativa, artefato, token e prazo a cada uso, na mesma transação que
lê bytes ou assina URL. Mudanças de configuração e retenção serializam com esse transporte. No
Baserow, uma intenção antiga conserva a convergência existente para a tentativa atual válida da
mesma linha; nunca recupera PDF ou claims antigos quando a atual está pendente ou inválida.
URLs de storage já emitidas e cópias externas conservam sua janela de validade; revogação protege
novos acessos pela aplicação e novas emissões, sem revogação retroativa desses bytes.
