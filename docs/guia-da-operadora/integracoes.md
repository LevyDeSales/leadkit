# Configurar e acompanhar integrações

[Sumário do guia](README.md) · [Acompanhar respostas](acompanhar-respostas.md) · [Encaminhamento](encaminhamento-e-jornada.md)

## Escolher quem recebe os dados

**Onde entrar:** “Integrações” (`/admin/integrations`).

Baserow envia dados para uma tabela. Webhook envia eventos a um serviço receptor. Nenhum deles é o botão de continuidade da participante: esse destino fica na conclusão do formulário.

1. Use “Conector” e “Formulário” e clique em “Filtrar” para localizar a associação.
2. Confira finalidade, eventos, revisão e situação. No Baserow, confira também se há mapeamento para “Captura sem PDF” ou “Plano com PDF”, conforme a finalidade publicada.
3. Leia “Última confirmação de envio”: a data pode corresponder à revisão atual, anterior ou não registrada. Um sucesso antigo não comprova que a configuração atual funciona.
4. Abra “Configurar”, “Mapear”, “Abrir teste”, “Revisar ativação” ou “Rever envios”, conforme as ações disponíveis.

**Resultado:** você navega aos detalhes sem fazer envios. “Abrir teste” apenas abre a seção. Os botões de mandar teste fazem envio externo. Uma conexão ativa significa envio habilitado; não garante compatibilidade atual com o provedor. Os contadores do catálogo são globais, mesmo quando há filtros; o resumo do receptor padrão é compartilhado, não deve ser somado às linhas dos formulários.

**Se houver credencial ilegível ou perfil incompatível:** abra os detalhes e peça ajuda técnica antes de ativar ou reenviar. A central mostra fatos locais conservados, não uma consulta completa de saúde do serviço externo.

## Conectar uma tabela Baserow

**Objetivo:** gravar respostas e dados autorizados para uso interno. Combine tabela e permissões com a administradora técnica antes de conectar.

**Onde entrar:** formulário → “Integração com o Baserow” ou central → “Configurar”. O formulário precisa ter versão publicada para o mapeamento.

1. Em “Finalidade da conexão”, escolha “Interna — respostas e dados comerciais”. Baserow não aceita finalidade participante.
2. Preencha “Endereço do Baserow” com a base fornecida pela administradora, “Número da tabela” e “Token de banco de dados”. Esse token precisa permitir ler, criar e atualizar linhas da tabela. Não é sua senha de login.
3. Clique em “Conectar”. O sistema consulta os campos; se o provedor recusar, a conexão não é guardada. Em conexão existente, “Salvar a conexão” preserva o token quando o campo fica vazio. Trocar de servidor exige um token para esse servidor.
4. No topo, escolha “Captura sem PDF” ou “Plano com PDF”. Em “Mapeamento”, relacione cada campo da tabela ao que deve receber. Inclua o identificador exigido no campo de texto: no perfil PDF, o “código do pedido” permite localizar e atualizar a mesma linha; no perfil de captura, use o “código da submissão”.
5. Clique em “Salvar o mapeamento” e confira “Relatório antes de ativar”. Corrija campos ausentes e opções sem par; as opções de seleção devem corresponder ao texto mostrado à participante. Perfis de captura e PDF são separados na mesma conexão.
6. Quando houver autorização para testar o receptor, clique em “Enviar a linha de teste”. É um envio externo sintético: a linha permanece na tabela; repetir atualiza a mesma linha de teste. Confira-a no Baserow.
7. Quando estiver pronta para habilitar envios, clique em “Ativar a integração”. Abra novamente a central e confira a situação e os próximos resultados.

**Resultado:** capturas passam a ser enviadas após as respostas serem salvas; pedidos PDF são sincronizados quando concluídos. O envio ocorre em segundo plano. A ativação não importa automaticamente todo o histórico anterior. Não reprocese pedidos apenas para preencher tabela antiga sem avaliar o custo e os efeitos.

**Limites do endereço:** até 2000 caracteres, sem espaços, usuário ou senha, sem trecho iniciado por `?` ou `#`. Use HTTPS; em produção, HTTP é recusado. Endereços literais de metadados de servidor são recusados. Peça à administradora um endereço alcançável pelos serviços da instalação; não tente contornar uma recusa de endereço.

**Se houver erro:** confira conexão, permissão do token, tabela, perfil e relatório. Abrir a página consulta os campos atuais no Baserow; mapeamento salvo não garante que a tabela não mudou. Mudar endereço ou tabela apaga o mapeamento e desativa a integração; mudar só o token mantém a configuração. “Desativar” interrompe a habilitação de envios sem remover a conexão.

## Configurar um receptor Webhook

**Onde entrar:** central → “Gerenciar webhooks” (`/admin/destinations`) → destino do formulário ou padrão.

1. Decida com a administradora se será um receptor próprio ou o padrão. Alterar o padrão também afeta outros formulários que o utilizam.
2. Preencha “URL do receptor” e escolha “Finalidade do receptor”: “Participante — plano público” para a saída autorizada à participante; “Interna — dados comerciais e respostas” para um serviço autorizado a receber informações internas. A audiência define quais dados podem sair; não a escolha só para superar um erro.
3. Em “Eventos aceitos pelo receptor”, marque apenas os contratos suportados: “Planos concluídos (PDF)”, “Liberação individual do plano” e/ou “Capturas persistidas (sem PDF)”. A administradora deve confirmar a compatibilidade.
4. Clique em “Criar o destino” ou “Salvar o endereço”. Não há um botão separado de ativação como no Baserow: a configuração pode passar a valer para os envios aplicáveis.
5. Na criação, o segredo aparece uma única vez. Guarde-o em local seguro e faça a configuração do receptor com a administradora. Não o coloque em mensagens, documentos públicos ou endereço de página.
6. Quando houver autorização para enviar teste, escolha “Finalidade do teste sintético” e clique em “Mandar o evento de teste”. O teste de captura só é oferecido se esse evento estiver declarado. Ele não cria uma submissão real.

**Resultado:** eventos aplicáveis passam ao receptor configurado; sem destino próprio, pode ser usado o padrão. Para modo manual, a entrega participante depende da liberação. Um receptor interno pode receber dados que não pertencem à saída pública; não o encaminhe diretamente à participante.

**Limites da URL:** até 2000 caracteres, endereço completo com servidor, sem usuário, senha, espaços ou fragmento `#`. Prefira HTTPS. HTTP é aceito pelo serviço, mas em produção exige confirmação explícita de que os dados irão sem criptografia; envolva a administradora nessa exceção. Endereços literais de metadados de servidor são recusados. URL aceita não comprova conectividade: teste saindo da aplicação não garante que o processo de entrega também alcance o receptor.

**Se o teste falhar:** confira resposta, finalidade, eventos e receptor com a administradora. Uma resposta de sucesso confirma apenas aquele transporte. Criar ou salvar não prova que a participante recebeu uma mensagem.

## Recuperar uma entrega com falha

**Onde entrar:** central para localizar o problema; depois detalhe do pedido, da captura ou integração Baserow.

1. Leia o erro e corrija a causa antes de reenviar. Use a última confirmação e sua revisão para avaliar se ela ainda representa a configuração atual.
2. Em pedido PDF, “Entregas” permite “Confirmar e reenviar” para webhook com falha elegível. Confira finalidade, revisão e tentativa mostradas. Entregas antigas ou de destino removido podem não permitir reenvio.
3. Em “Sincronizações com a planilha”, “Reenviar agora” repete a sincronização com falha. Na configuração Baserow, “Reenviar as que falharam” trata até 200 por ação; confira o alcance antes de clicar.
4. Em captura, abra “Entregas às integrações”, leia o destino e a revisão, marque a confirmação e use “Repetir entrega” quando oferecido. Mantém o mesmo evento e destino.
5. Reabra o registro e confira se ficou pendente, confirmado ou com falha novamente.

**Resultado:** repetição do envio externo, sem gerar um novo plano. Isso é diferente de “Reprocessar pedido”, que cria tentativa e chamada de inteligência artificial. Uma confirmação de transporte não prova leitura, reunião, atendimento ou liberação do PDF.

**Se não houver reenvio disponível:** confira tentativa, destino removido e revisão; peça ajuda técnica. Sem registro de entrega aplicável, não presuma que ativar agora recuperará o passado. Não use novo envio de respostas como recuperação de uma falha externa.

## Trocar segredo ou remover uma configuração

“Trocar o segredo…” → “Gerar um segredo novo” invalida o segredo anterior imediatamente. Combine a troca no receptor antes de confirmar e guarde o novo valor mostrado uma vez. Alterar a URL do webhook mantém o segredo; entregas PDF já fixadas não são automaticamente redirecionadas. Uma entrega fixada que exija a revisão atual precisa de reenvio explícito autorizado. Em alerta de chave de proteção ausente ou credencial ilegível, procure a administradora; não tente recriar a chave de proteção da instalação.

“Remover o destino” pode fazer novos eventos de um formulário usarem o padrão; sem ele, não nasce entrega. As entregas PDF já fixadas não migram para outro receptor. Registros de entrega de captura desse destino são apagados, inclusive pendentes, sem transferência ao padrão; as respostas permanecem.

“Remover a integração” Baserow apaga token, estado das sincronizações e registros de entrega de captura informados na confirmação. Linhas já recebidas no Baserow ficam lá, e respostas salvas permanecem. Leia as contagens antes de marcar a confirmação. Remover uma integração não apaga as cópias que já chegaram ao serviço externo.
