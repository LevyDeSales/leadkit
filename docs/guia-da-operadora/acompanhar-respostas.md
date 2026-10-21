# Acompanhar respostas, PDFs e acesso

[Sumário do guia](README.md) · [Integrações](integracoes.md) · [FAQ](perguntas-frequentes.md)

## Encontrar respostas recebidas

**Onde entrar:** “Capturas” para formulários sem PDF; “Pedidos” para formulários com geração.

Na lista de “Formulários”, a coluna “Envios” pode incluir capturas, mas seu link abre apenas “Pedidos”. Para respostas sem PDF, entre diretamente em “Capturas”.

Na captura (`/admin/captures`), preencha “Formulário” com o endereço identificador do formulário e, se necessário, “Número da versão”; clique em “Filtrar capturas”. Abra a linha desejada. O detalhe mostra identificação, respostas, consentimento e pontuação, atualmente reunidos também em texto JSON. Não é uma falha de exibição: peça ajuda para interpretar os nomes se necessário.

Em “Pedidos” (`/admin`), use “Buscar” por nome, e-mail ou código e os filtros “Formulário”, “Versão”, “Recebido”, “Status”, “Entrega” e “Planilha”. A versão aparece quando um formulário foi escolhido. Abra o nome na linha para ler “Respostas do formulário”, “Contato e consentimentos” e a “Linha do tempo”.

**Resultado:** você encontra o envio e sua versão, sem alterar o conteúdo respondido. No detalhe, “Jornada” abre os fatos locais e as declarações externas.

**Se não encontrar:** limpe filtros, confira a finalidade e o ambiente. Os registros podem ter saído pela retenção. Contagens e ausência de linhas não comprovam que ninguém respondeu no passado.

## Conferir se o PDF atual existe e pode ser acessado

**Onde entrar:** detalhe do pedido → “PDF da tentativa atual” e “Acesso da participante”.

1. Confira o número da “Tentativa” no topo. Cada reprocessamento cria outra tentativa.
2. Leia o estado em “PDF da tentativa atual”. Use “Baixar PDF” quando disponível. Se aparecer “Sem verificação disponível”, use “Verificar e baixar PDF atual” para tentar conferir o arquivo.
3. Confira separadamente o acesso: “Retido”, “Liberado para esta tentativa” ou “Automático”.
4. Para revisar a saída pública, use “Ver como participante”; o plano em “Plano gerado · uso interno” pode conter dados que não devem ser compartilhados. “Ver como participante” mostra atualmente o documento público em JSON dentro do painel; não abre o link público, não concede acesso e não envia o plano.

| Estado do arquivo | O que fazer |
| --- | --- |
| “Disponível” | O arquivo da tentativa atual foi conferido. Ainda é preciso verificar a permissão da participante. |
| “Arquivo ausente” | Não há arquivo atual disponível; confira processamento e peça investigação técnica se deveria existir. |
| “Removido pela retenção” | O arquivo foi apagado pelo prazo de guarda; o histórico de conclusão pode continuar visível. |
| “Sem verificação disponível” | Falta prova atual; tente verificar pelo botão e procure ajuda se persistir. |
| “Arquivo divergente” | O arquivo não corresponde ao registrado; peça ajuda técnica. |
| “Indisponível no momento” | Há impedimento de acesso; aguarde ou peça verificação técnica antes de gerar novamente. |

**Resultado:** você distingue processamento concluído, arquivo disponível e acesso autorizado. Consultar ou baixar não dispara outra chamada de geração. Um PDF listado em “PDFs” pode ser de uma tentativa anterior; ele não substitui a disponibilidade atual.

**Se a participante não acessar:** confira liberação, disponibilidade e a validade do “Link do lead”, além do ambiente e do pedido corretos. Acesso liberado não renova arquivo apagado nem link vencido. Não envie a ela uma saída interna como atalho.

## Liberar manualmente uma tentativa

**Onde entrar:** pedido → “Acesso da participante”.

A liberação manual só vale para versões que optaram explicitamente por “Manual” no rascunho e foram publicadas. Versões sem essa escolha conservam o comportamento automático legado. Alterar o formulário hoje não muda a política histórica de um pedido anterior. Quando a política histórica não pode ser conferida, o sistema protege o acesso; peça investigação técnica.

1. Confira que o acesso está retido e que o PDF atual está disponível. Nessas condições, a área de liberação apresenta o formulário.
2. Escolha “Modo de liberação”: “Revisão padrão” ou “Outra jornada”. Não é obrigatório haver compra ou reunião.
3. Escolha o “Motivo” que corresponde à decisão.
4. Marque “Confirmo a liberação desta tentativa” e clique em “Liberar acesso”.
5. Confira a indicação de acesso liberado e, separadamente, as entregas ou notificações registradas.

**Resultado:** a autorização fica registrada para essa tentativa e esse arquivo. A notificação depende de um canal participante configurado; liberação não é entrega, recebimento ou leitura. Evento externo de compra, reunião ou presença não libera por conta própria.

**Se houver conflito:** reabra o pedido e confira a tentativa atual antes de repetir. Se outra tentativa foi criada ou o arquivo não está disponível, não use dados antigos para confirmar. Em modo manual, reprocessar fecha o acesso da nova tentativa até outra liberação.

## Tratar falha de geração

**Onde entrar:** pedido → “Linha do tempo” e “Reprocessar…”.

1. Confira se a falha é de geração. Se o plano está pronto e só o envio externo falhou, siga [Reenviar integração](integracoes.md).
2. Peça a correção da causa à administradora técnica quando envolver fila, serviço de geração, modelo ou armazenamento.
3. Quando a causa estiver resolvida e você tiver decidido gerar novamente, abra “Reprocessar…”. Essa ação só fica disponível após o pedido terminar, com sucesso ou falha.
4. Informe “Motivo” de 10 a 300 caracteres, marque a confirmação e clique em “Reprocessar pedido”.
5. Acompanhe a nova tentativa na linha do tempo.

**Resultado:** uma nova tentativa e uma nova chamada de inteligência artificial, com possível custo. O PDF anterior continua no histórico, dentro dos prazos de retenção. Em acesso automático, a participante verá o novo plano quando pronto; em manual, a nova tentativa exigirá liberação.

**Se o botão não aparecer:** confira se ainda está processando. Não peça à participante para preencher tudo de novo para corrigir uma fila ou entrega. A execução técnica pode ser consultada por quem administra o sistema no link “Ver execução no Hatchet”, quando disponível.

## Conferir prazos de guarda

**Onde entrar:** formulário → “Retenção” → “Mudar os prazos…”.

Leia os prazos antes de propor alteração. “Arquivo do PDF” trata a guarda do arquivo; “Pedido inteiro” trata respostas, plano, PDF e contato quando não há outro pedido. Campos vazios usam o padrão da instalação. Alterar prazo não exige publicar e alcança pedidos existentes. A rotina roda diariamente e respeita o vencimento do link da participante.

Se a mudança estiver aprovada, preencha os prazos e clique em “Salvar os prazos”. Em redução, leia o impacto, marque a confirmação e use “Confirmar e salvar os prazos”. O que for apagado não volta. Se o impacto não estiver claro, não confirme e consulte a responsável. Aumentar um prazo depois não recupera o que já foi excluído.
