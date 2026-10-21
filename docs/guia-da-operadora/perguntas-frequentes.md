# Perguntas frequentes e busca por problema

[Sumário do guia](README.md)

## Salvei, mas o formulário público não mudou

“Salvar o rascunho” preserva a edição para revisão. Faça os testes indicados e [publique uma nova versão](testar-e-publicar.md). Confira também ambiente e situação ativa. A prévia de PDF pode mostrar campos não salvos; vê-los ali não comprova salvamento.

## Criei um formulário, mas o endereço não abre

Antes da primeira publicação não há formulário público disponível. Depois, ele precisa estar ativo. Confira “Versão publicada” e “Situação” na página do formulário. Não use “Importar” como uma forma de rascunhar: essa tela publica diretamente.

## Só quero salvar respostas. Preciso configurar geração?

Não. Escolha captura e uma confirmação, botão ou encaminhamento, conforme o [começo rápido](comeco-rapido.md). Não escolha acompanhamento do PDF. Os envios aparecem em “Respostas” → “Somente respostas”, na lista de capturas, sem pedido de geração.

## O que posso testar sem enviar dados para fora?

A prévia da página, a prévia da conclusão, a validação local de captura, “Montar o prompt” e “Calcular a pontuação” não enviam respostas a um receptor externo. As prévias do PDF usam plano de exemplo ou teste já existente e não geram texto novo. “Testar com o LLM” chama a inteligência artificial. “Enviar a linha de teste” e “Mandar o evento de teste” fazem envio externo sintético; a linha Baserow permanece na tabela. Veja [Testes](testar-e-publicar.md) e [Integrações](integracoes.md).

## A participante viu “Respostas salvas”, mas não chegou ao outro site

O recibo confirma o envio salvo. Oriente a usar o botão de continuidade, sem reenviar o formulário. A página externa tem seu próprio funcionamento; peça verificação do destino. Encaminhamento não significa que os dados foram entregues a uma integração.

## O PDF está concluído, mas ausente ou indisponível

Confira “PDF da tentativa atual”. Conclusão histórica não prova que o arquivo ainda existe, está íntegro ou pode ser lido. Arquivo de tentativa anterior não substitui o atual. Consulte [Disponibilidade do PDF](acompanhar-respostas.md); verificar não chama novamente a inteligência artificial.

## O PDF está pronto, mas a participante não pode abrir

Confira “Acesso da participante”, validade do link e arquivo atual. Em modo manual, é necessário [liberar a tentativa](acompanhar-respostas.md). As versões antigas sem escolha manual conservam o automático. Configurar modo manual hoje não altera pedidos históricos. Link vencido ou arquivo apagado não volta com uma liberação.

## Liberar acesso envia mensagem? Uma reunião libera o plano?

Liberação é autorização de acesso. Notificação depende do canal participante configurado e tem estado próprio de entrega. Reunião, compra, convite, presença ou declaração externa não liberam o plano automaticamente. Leia [Jornada](encaminhamento-e-jornada.md).

## Preciso reprocessar quando só a integração falhou?

Use o [reenvio da entrega ou sincronização](integracoes.md). “Reprocessar pedido” gera nova tentativa, nova chamada de inteligência artificial e pode ter custo; em modo manual, também exige nova liberação. Não é um botão de atualizar a página.

## A integração está ativa e tem último sucesso. Está tudo certo?

Confira a revisão do último sucesso, o perfil atual e o relatório no detalhe. Ativa significa habilitada; sucesso pode pertencer à configuração anterior. Confirmação de transporte não comprova leitura, atendimento ou liberação. Sem confirmação registrada, não conclua que a configuração nunca funcionou: o histórico local pode ser limitado.

## Troquei a imagem e o PDF antigo continua igual

É o comportamento esperado: cada versão publicada fixa a imagem. Confira a prévia e publique uma nova versão para os próximos envios usarem a troca. O PDF dos pedidos anteriores permanece associado à versão deles. Veja [Imagens](imagens-e-pdf.md).

## Voltar a uma versão antiga coloca essa versão no ar?

“Copiar para o rascunho” substitui o rascunho pela definição escolhida. Ainda precisa revisar e publicar; a publicação cria uma nova versão. Essa cópia não recupera integrações ou segredos excluídos.

## Apagar dados externos apaga o pedido ou as cópias no outro serviço?

Não. A ação da jornada apaga identificadores e evidências externos locais e encerra o encaminhamento individual; respostas e permissão do PDF são separadas. Cópias no outro serviço seguem o processo de lá. Remover integração também não apaga linhas já recebidas no Baserow. [Confira o alcance antes de confirmar](encaminhamento-e-jornada.md).

## Quando chamar a administradora técnica?

Quando faltar acesso; a sessão ou o bloqueio não se resolverem; houver credencial ilegível, fila parada, falha de geração, armazenamento, arquivo divergente ou problema de conectividade. Peça ajuda também para fórmulas, JSON, templates, permissões de fontes e receptores que você não saiba revisar. Informe ambiente, versão, caminho da tela, código do pedido ou captura e mensagem segura do erro em canal autorizado; não envie senha, chave, token ou conteúdo completo da participante em mensagens abertas.
