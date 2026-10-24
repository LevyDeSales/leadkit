# Configurar e acompanhar integrações

[Sumário do guia](README.md) · [Acompanhar respostas](acompanhar-respostas.md) · [Encaminhamento](encaminhamento-e-jornada.md)

## Escolher quem recebe os dados

**Onde entrar:** “Integrações” (`/admin/integrations`).

Baserow envia dados para uma tabela. Webhook envia eventos a um serviço receptor. Nenhum deles é o botão de continuidade da participante: esse destino fica na conclusão do formulário.

1. Em “Para onde vão as respostas?”, escolha “Formulário” e, se necessário, “Conector”; clique em “Filtrar”. A seleção só consulta registros locais, sem acessar o fornecedor, ativar conexões ou fazer envios.
2. Confira finalidade, eventos, revisão e situação. No Baserow, confira também se há mapeamento para “Captura sem PDF” ou “Plano com PDF”, conforme a finalidade publicada.
3. Leia “Última confirmação de envio”: a data pode corresponder à revisão atual, anterior ou não registrada. Um sucesso antigo não comprova que a configuração atual funciona.
4. Leia “Próximo passo”. A indicação prioriza receptor ou credencial a corrigir, perfil e mapeamento pendentes, depois falhas registradas no perfil do formulário. Uma falha histórica pede revisão; as contagens não dizem que a revisão atual falhou. “Outras ações” conserva os caminhos para configuração, mapeamento, teste, ativação e envios quando disponíveis.
5. No Baserow, leia o aviso antes de clicar em “Consultar campos da planilha no Baserow”: essa ação abre a configuração na seção indicada e consulta os campos atuais ao vivo. O perfil “Captura sem PDF” ou “Plano com PDF” é preservado quando disponível. Não envia linha de teste nem ativa a conexão.

**Resultado:** a central e seus filtros mostram fatos locais. Abrir o detalhe Baserow faz a consulta ao vivo descrita acima, sem enviar respostas. “Abrir teste” apenas abre a seção. Os botões de mandar teste fazem envio externo. Uma conexão ativa significa envio habilitado; não garante compatibilidade atual com o provedor. “Sem verificação registrada” não significa teste aprovado; a confirmação de transporte é mostrada separadamente. Se a linha mostra “Usa padrão”, existe um receptor efetivo herdado: alterar esse padrão pode afetar outros formulários. Os contadores em “Conectores e contadores globais” são globais, mesmo quando há filtros; o resumo do receptor padrão é compartilhado, não deve ser somado às linhas dos formulários.

**Se houver credencial ilegível ou perfil incompatível:** abra os detalhes e peça ajuda técnica antes de ativar ou reenviar. A central mostra fatos locais conservados, não uma consulta completa de saúde do serviço externo.

## Conectar uma tabela Baserow

**Objetivo:** gravar respostas e dados autorizados para uso interno. Combine tabela e permissões com a administradora técnica antes de conectar.

**Onde entrar:** formulário → “Integração com o Baserow” ou central → “Consultar campos da planilha no Baserow”. O formulário precisa ter versão publicada para o mapeamento.

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
3. Em “Eventos aceitos pelo receptor”, marque apenas os contratos suportados: “Planos concluídos (PDF)”, “Liberação individual do plano”, “Capturas persistidas (sem PDF)” ou, no destino próprio do formulário, “Enviar respostas ao concluir o formulário”. Para esta última opção, siga os passos abaixo; ela exige finalidade interna e não pode ser selecionada junto com “Capturas persistidas (sem PDF)”. A administradora deve confirmar a compatibilidade.
4. Clique em “Criar o destino” ou “Salvar o endereço”. Não há um botão separado de ativação como no Baserow: a configuração pode passar a valer para os envios aplicáveis.
5. Na criação, o segredo aparece uma única vez. Guarde-o em local seguro e faça a configuração do receptor com a administradora. Não o coloque em mensagens, documentos públicos ou endereço de página.
6. Quando houver autorização para enviar teste, escolha “Finalidade do teste sintético” e clique em “Mandar o evento de teste”. “Respostas após salvar” testa a nova opção; “Captura persistida (sem submissão real)” testa a captura legada. Cada teste de respostas só é oferecido quando o evento correspondente está selecionado e salvo. É um envio externo de dados fictícios, sem criar uma submissão real.

**Resultado:** eventos aplicáveis passam ao receptor configurado; sem destino próprio, pode ser usado o padrão. Para modo manual, a entrega participante depende da liberação. Um receptor interno pode receber dados que não pertencem à saída pública; não o encaminhe diretamente à participante.

**Limites da URL:** até 2000 caracteres, endereço completo com servidor, sem usuário, senha, espaços ou fragmento `#`. Prefira HTTPS. HTTP é aceito pelo serviço, mas em produção exige confirmação explícita de que os dados irão sem criptografia; envolva a administradora nessa exceção. Endereços literais de metadados de servidor são recusados. URL aceita não comprova conectividade: teste saindo da aplicação não garante que o processo de entrega também alcance o receptor.

**Se o teste falhar:** confira resposta, finalidade, eventos e receptor com a administradora. Uma resposta de sucesso confirma apenas aquele transporte. Criar ou salvar não prova que a participante recebeu uma mensagem.

## Enviar respostas ao salvar, com ou sem PDF

**Onde entrar:** “Integrações” → “Gerenciar webhooks” → “Formulários” → “Abrir” na linha do formulário (`/admin/destinations/forms/{slug}`, substituindo `{slug}` pelo identificador do formulário).

1. Abra o destino próprio do formulário. A opção não está disponível no destino padrão; herdar o padrão não habilita esse envio.
2. Preencha “URL do receptor” e escolha “Interna — dados comerciais e respostas” em “Finalidade do receptor”. Combine antes com a administradora se o serviço está autorizado e preparado para receber respostas e consentimentos internos.
3. Em “Eventos aceitos pelo receptor”, marque “Enviar respostas ao concluir o formulário” e desmarque “Capturas persistidas (sem PDF)”. São duas opções exclusivas para o envio das respostas. “Planos concluídos (PDF)” e “Liberação individual do plano” continuam sendo eventos separados, se também forem necessários e suportados.
4. Clique em “Criar o destino” ou “Salvar o endereço” e, na criação, guarde o segredo como descrito acima. **Salvar já habilita o envio externo aplicável às próximas respostas salvas**; não há ativação separada nem envio automático do histórico anterior.
5. Para conferir o receptor com autorização, em “Evento de teste” escolha “Respostas após salvar” na “Finalidade do teste sintético” e clique em “Mandar o evento de teste”. Esse botão envia dados fictícios ao serviço externo. É possível testar antes da primeira publicação, com um rascunho válido; não é preciso publicar para testar.
6. Se aparecer “Evento de teste não enviado” por rascunho inválido, leia a orientação, corrija e salve o rascunho, ou peça ajuda técnica, antes de repetir. Não publique o formulário para contornar o erro.

**Resultado:** depois que as respostas forem salvas, o envio ocorre em segundo plano, tanto em captura sem PDF quanto em formulário com PDF. Um PDF pendente, uma geração que falhou ou um plano retido não impedem o envio das respostas. Esse evento não entrega o PDF nem libera seu acesso. Falha de transporte não desfaz as respostas salvas e não exige novo preenchimento.

Essa escolha se aplica ao webhook interno. O Baserow conserva seus perfis: captura após salvar, pedido PDF após concluir a geração. Os eventos legados de PDF também conservam seu momento de envio; não passam a enviar respostas antecipadamente por causa desta opção.

## Recuperar uma entrega com falha

**Onde entrar:** central para localizar o problema; depois detalhe do pedido, da captura ou integração Baserow.

1. Leia o erro e corrija a causa antes de reenviar. Use a última confirmação e sua revisão para avaliar se ela ainda representa a configuração atual.
2. Para respostas enviadas após salvar em um pedido PDF, abra “Respostas” → “Com PDF” → pedido → “Respostas salvas”. Confira “Sincronização pendente”, “Sincronização concluída” ou “Sincronização falhou”, o destino, a revisão original e a atual. Havendo falha e botão disponível, marque “Confirmo repetir o envio externo ao mesmo destino e revisão autorizada.” e clique em “Repetir envio de respostas”. O envio mantém o mesmo evento, conteúdo, destino e revisão original; não gera nem libera PDF.
3. Para os eventos legados do pedido PDF, use “Entregas” → “Confirmar e reenviar” quando houver falha elegível. Confira finalidade, revisão e tentativa mostradas. Essa área é separada de “Respostas salvas”; entregas antigas ou de destino removido podem não permitir reenvio.
4. Em “Sincronizações com a planilha”, “Reenviar agora” repete a sincronização Baserow com falha. Na configuração Baserow, “Reenviar as que falharam” trata até 200 por ação; confira o alcance antes de clicar.
5. Para captura, abra “Respostas” → “Somente respostas” → captura → “Entregas às integrações”. Leia destino, estado e tentativas; os estados aparecem como `pending` (pendente), `succeeded` (transporte confirmado) ou `failed` (falha). Quando oferecido, marque a confirmação de envio externo e use “Repetir entrega”. Na nova opção, a revisão precisa continuar igual à original; o reenvio conserva o mesmo evento e destino.
6. Reabra o registro e confira se ficou pendente, confirmado ou com falha novamente.

**Resultado:** repetição do envio externo, sem gerar um novo plano. Isso é diferente de “Reprocessar pedido”, que cria tentativa e chamada de inteligência artificial. Uma confirmação de transporte não prova leitura, reunião, atendimento ou liberação do PDF.

**Se não houver reenvio disponível:** confira tentativa, destino removido, revisão e retenção; peça ajuda técnica. Para “Enviar respostas ao concluir o formulário”, mudar URL, segredo ou revisão bloqueia as pendências da configuração original: elas não podem ser repetidas com a nova revisão. Não troque de formulário nem peça outra resposta para recuperar essas entregas. Sem registro aplicável, habilitar agora não recupera o passado; dados excluídos não são recriados.

## Trocar segredo ou remover uma configuração

“Trocar o segredo…” → “Gerar um segredo novo” invalida o segredo anterior imediatamente. Combine a troca no receptor antes de confirmar e guarde o novo valor mostrado uma vez. Alterar a URL do webhook mantém o segredo. Em “Enviar respostas ao concluir o formulário”, trocar URL, segredo ou revisão impede os envios ainda pendentes na revisão original. Eles não migram para a configuração nova, não podem ser repetidos nela e não são recuperados criando outro formulário. A configuração nova vale para próximas respostas salvas. Nos eventos legados de PDF, entregas já fixadas não são automaticamente redirecionadas; quando aplicável, exigem reenvio explícito autorizado com a revisão atual, preservando a finalidade. Em alerta de chave de proteção ausente ou credencial ilegível, procure a administradora; não tente recriar a chave de proteção da instalação.

“Remover o destino” pode fazer novos eventos de um formulário usarem o padrão; sem ele, não nasce entrega. As entregas PDF já fixadas não migram para outro receptor. Registros de entrega de respostas desse destino, com ou sem PDF, são apagados, inclusive pendentes, sem transferência ao padrão; as respostas permanecem.

“Remover a integração” Baserow apaga token, estado das sincronizações e registros de entrega de captura informados na confirmação. Linhas já recebidas no Baserow ficam lá, e respostas salvas permanecem. Leia as contagens antes de marcar a confirmação. Remover uma integração não apaga as cópias que já chegaram ao serviço externo.
