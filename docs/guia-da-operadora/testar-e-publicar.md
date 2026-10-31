# Testar, publicar e recuperar versões

[Sumário do guia](README.md) · [Configurar](configurar-formulario.md) · [Acompanhar respostas](acompanhar-respostas.md)

## Conferir sem receber um envio real

**Onde entrar:** rascunho salvo do formulário.

1. Abra “Ver a página no celular”. Percorra as etapas e marque respostas diferentes para conferir as condições. Nada é enviado, e essa prévia não valida as respostas. Abrir uma etapa pela lista reinicia as marcações.
2. Em “Depois de salvar as respostas”, abra “Conferir conclusão salva”. Confira mensagem, rótulo e destino; o botão fica desativado e não há redirecionamento externo.
3. Para PDF, confira também [imagens e aparência](imagens-e-pdf.md).

**Resultado:** você confere a apresentação sem criar um pedido ou captura. Uma prévia correta ainda não comprova que a geração funciona.

**Se algo não refletir sua edição:** volte e salve. A prévia das perguntas e da conclusão usa o rascunho salvo.

## Validar somente captura

**Onde entrar:** rascunho com “Somente salvar respostas” → “Testar o rascunho”.

1. Confira a origem “Rascunho salvo” e preencha os campos com respostas fictícias. Perguntas e alternativas seguem as condições do formulário; não é necessário consentimento real.
2. Escolha “Data simulada”, se precisar conferir regras de uma data específica.
3. Clique em “Validar captura”.
4. Confira “Respostas válidas”, pontuação e mensagem de conclusão; use “Conferir conclusão” para a apresentação.

**Resultado:** validação local, sem inteligência artificial, PDF, envio persistido ou entrega a uma integração. O teste não aparecerá como uma captura recebida.

**Se recusar:** siga os links dos campos com erro e corrija as respostas ou a data. Os valores permanecem na tela. Se a origem salva mudou, confira suas respostas e use “Abrir o rascunho salvo atual” antes de tentar novamente.

## Testar a geração do PDF

**Onde entrar:** rascunho com geração de PDF → “Testar o rascunho”.

1. Confira “Rascunho salvo”, preencha os campos com respostas fictícias e escolha “Data simulada” quando necessário. Campo vazio significa sem resposta; não carrega exemplos automaticamente.
2. Use “Montar o prompt” para ler o que será enviado à inteligência artificial e “Calcular a pontuação” para conferir as notas. Essas ações não fazem a chamada de geração.
3. Quando estiver pronta para um teste real, marque a confirmação da chamada de IA e do possível custo e clique em “Gerar plano de teste com IA”. Ele chama o serviço de inteligência artificial, pode ter custo e inclui as rodadas de reparo necessárias; também gera o PDF. Há um limite de um teste por vez e uma pausa entre testes.
4. Aguarde o resultado, abra o teste em “Testes recentes” e, quando disponível, use “Baixar o PDF do teste”. Confira o plano e a apresentação.

**Resultado:** um teste guardado temporariamente, sem criar pedido nem mandar webhook. A tela informa quantos testes e por quantos dias são conservados. O plano mostrado no detalhe do teste inclui os campos internos; não o compartilhe como se fosse a saída da participante.

**Se falhar:** leia a mensagem e o que foi enviado. Corrija e salve antes de testar de novo. Se a infraestrutura estiver indisponível, peça ajuda técnica; não repita cliques esperando acelerar a fila. Um teste antigo pode não cobrir as mudanças atuais.

## Recuperar um teste sem gastar outro

Ao abrir o teste, a página conserva o endereço da ação. Depois de confirmar, você recebe o
“Recibo do teste”, com o estado da criação, do enfileiramento e do resultado. Se a resposta se
perder, recarregue esse endereço ou reenvie o mesmo formulário: ele recupera o teste original,
sem outra chamada de geração. A recuperação funciona também sem JavaScript.

“Despacho incerto” significa que ainda não há confirmação de recebimento pela infraestrutura;
não significa custo zero. Atualize o recibo para acompanhar. Se o resultado já foi removido,
“Resultado expirado” conserva a identificação original enquanto o recibo estiver disponível.
O recibo fica disponível por três dias após a criação e a identidade antiga não inicia outro
teste depois disso. Esses prazos técnicos não são uma autorização reutilizável.

Para iniciar outra execução, escolha **Novo teste**, confira a revisão e as respostas e confirme
novamente o possível custo. Não escolha Novo teste apenas porque a resposta anterior demorou:
primeiro confira o recibo. Um teste novo continua sujeito à ocupação e à pausa de 30 segundos.

## Usar respostas JSON no modo avançado

Abra “Avançado: respostas JSON” e selecione “Usar somente o JSON abaixo”. Esse modo ignora os campos visuais: as entradas não são mescladas. JSON em branco usa explicitamente os exemplos salvos; `{}` representa respostas vazias e pode falhar nas perguntas obrigatórias. Para voltar, selecione “Preencher campos”. A data simulada vale para o modo escolhido.

Com teclado, use Tab para percorrer campos e ações; nos grupos, use as teclas de seleção do navegador. Sem JavaScript, todas as perguntas aparecem, mas o servidor considera apenas as perguntas visíveis pelas condições e recusa alternativas indisponíveis. Erros têm links para os campos e preservam a entrada. Uma origem alterada pede revisão antes de uma nova tentativa; salvar o editor continua sendo uma ação separada.

## Publicar uma versão

**Objetivo:** colocar a definição revisada em uso para novos envios. Siga este procedimento quando a publicação estiver decidida pela responsável.

**Onde entrar:** rascunho → “Publicar…”. Abrir esse link apenas mostra a revisão; publicar exige o botão final.

1. Confira “O que muda em relação à versão” ou “Primeira publicação”. Revise perguntas, conclusão, imagens e integrações.
2. Preencha “Nota do que mudou”. Ela é obrigatória.
3. Confira “Teste com o LLM”. Para PDF, a primeira publicação e as mudanças que afetam a geração exigem teste bem-sucedido correspondente. Captura não exige teste de inteligência artificial. Mudanças só de aparência não implicam sempre um novo teste de geração; siga a indicação da tela.
4. Se houver aviso de envio de contato no prompt, revise a necessidade com a responsável. Confirme somente se o uso estiver aprovado; caso contrário, volte ao rascunho para retirar a referência.
5. Clique em “Publicar a versão” com o número mostrado. Aguarde a confirmação e confira “Versão publicada” no formulário.
6. Confira “Situação” e o endereço público. Publicar não reativa um formulário inativo. Divulgue o link apenas quando estiver publicado e ativo.

**Resultado:** uma nova versão que não pode ser editada. Os próximos envios passam a usá-la imediatamente quando o formulário está ativo. Pedidos existentes mantêm a versão respondida. Na primeira publicação, o endereço passa a ser fixo.

**Se a publicação for recusada:** siga a lista “A versão não foi publicada. Falta:”. Ela pode pedir nota, teste, correção da definição, imagem ou integração compatível. “Nada para publicar” significa que o rascunho já está igual à versão atual. Em conflito com outra alteração, reabra e compare; não presuma que o clique anterior falhou nem publique outra vez sem verificar.

## Pausar ou recuperar conteúdo anterior

**Onde entrar:** página do formulário.

Para interromper novos envios, abra “Desativar…”, leia o efeito, confirme e clique em “Desativar o formulário”. Isso torna o formulário indisponível para novos envios, mas não cancela pedidos já recebidos. “Ativar o formulário” permite receber novamente, desde que exista versão publicada.

Para recuperar conteúdo, vá a “Histórico”, confira “O que mudou” e abra “Voltar a esta versão…”. A confirmação e “Copiar para o rascunho” substituem o rascunho atual pela cópia escolhida; preserve mudanças que ainda queira manter. Nada muda na página pública nesse momento. Revise, teste quando exigido e publique: será criada uma nova versão, sem apagar o histórico.

**Se a versão antiga depender de uma integração ou imagem:** confira a compatibilidade atual antes de publicar. A cópia de uma definição não restaura serviços externos nem credenciais removidas.
