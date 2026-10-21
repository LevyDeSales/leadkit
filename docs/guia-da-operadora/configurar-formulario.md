# Configurar o formulário

[Sumário do guia](README.md) · [Criar e salvar](criar-e-salvar.md) · [Testar e publicar](testar-e-publicar.md)

As instruções abaixo explicam como operar o editor. O conteúdo das perguntas, das mensagens e do plano deve ser definido por você e pela responsável pelo conteúdo.

## Ajustar a página e as perguntas

**Onde entrar:** “Formulários” → seu formulário → “Editar o rascunho”.

1. Em “Página pública”, ajuste “Título”, “Introdução” e “Tempo para responder, em minutos” (de 1 a 60). Os avisos opcionais têm título, texto e link; são texto puro, sem formatação de código.
2. Em “Etapas e perguntas”, use “Adicionar etapa” para criar uma tela e “Adicionar pergunta” dentro dela.
3. Abra a pergunta e preencha “Rótulo”, que é o texto que a participante lê. Escolha “Tipo”, “Ajuda”, “Obrigatória” e os limites apresentados para esse tipo.
4. Confira “Nome da resposta”. Ele identifica a resposta nas integrações e nas instruções de geração. Evite mudá-lo após publicar sem revisar as dependências com a administradora técnica.
5. Nas perguntas de escolha, use “Adicionar opção”, preencha o rótulo e confira o valor. O valor é o que fica gravado; melhorar o rótulo não exige trocar esse identificador. Marque respostas de exemplo fictícias.
6. Use “Subir” e “Descer” para ordenar e confira o alcance de “Remover a pergunta” e “Remover a etapa”. Remover uma etapa retira também suas perguntas do rascunho.
7. Clique em “Salvar o rascunho”.

**Resultado:** perguntas prontas para conferir em “Ver a página no celular”. Dados de contato merecem cuidado: a opção “Dado de contato: fica fora do rascunho no aparelho” trata a cópia local no dispositivo, não uma autorização de envio a terceiros. E-mail e telefone já ficam fora dessa cópia local.

**Se houver erro:** corrija o campo indicado. Alterações de nome, valor ou ordem podem afetar condições, pontuação, modelo do plano e integrações; confira essas áreas antes de publicar.

## Mostrar uma pergunta ou opção somente em certas respostas

**Onde entrar:** no rascunho, abra a pergunta ou opção e sua área “Mostrar esta pergunta quando…” ou “Mostrar esta opção quando…”.

1. Use “Adicionar condição” quando a área estiver vazia.
2. Em “Vale quando”, escolha a combinação das regras apresentada na tela.
3. Escolha “Pergunta da regra”, “Operador da regra” e o valor correspondente. Os campos disponíveis mudam conforme a pergunta e a comparação escolhidas.
4. Use “Adicionar regra” se necessário. Para deixar a exibição sem essa restrição, use “Tirar a condição”.
5. Salve e abra “Ver a página no celular”. Marque respostas que mostram e que escondem a pergunta ou opção.

**Resultado:** a prévia permite percorrer os caminhos de exibição. Ela não valida um envio nem guarda as respostas marcadas.

**Se aparecer “revise a condição”:** escolha novamente a pergunta ou valor da regra indicada. Referências a algo removido ou renomeado impedem salvar. Se uma condição precisar de comparação que você não entende, peça ajuda; não a substitua por uma regra aproximada.

## Conferir identidade e consentimento

**Onde entrar:** “Identidade da lead” e “Consentimento”, no rascunho.

1. Relacione as perguntas usadas para identificar e-mail, nome e telefone conforme os campos apresentados. O e-mail é obrigatório na identidade.
2. Revise “Autorização do processamento” e “Aviso sobre o uso dos dados”.
3. Ofereça apenas os canais de contato necessários, com seus textos separados da autorização de processamento. A oferta de WhatsApp precisa de telefone na identidade.
4. Salve e confira o texto na prévia.

**Resultado:** a participante vê o texto e as opções correspondentes. A publicação atualiza a versão do consentimento quando seu texto muda; cada envio conserva a versão respondida. Marcar consentimento para contato não dispara uma mensagem.

**Se houver dúvida sobre uso de dados:** valide o texto e os destinatários com a responsável antes de publicar. A marcação de contato não garante que um dado esteja fora das instruções enviadas à inteligência artificial: revise também o prompt.

## Ajustar geração, pontuação e dados de apoio

**Onde entrar:** no rascunho, nas seções “Modelo do plano”, “Pontuação”, “Dados do formulário” e “Instruções, prompt e o resto”.

“Instruções” são as orientações fixas da geração. “Prompt” é o pedido montado com as respostas a cada envio. A tela usa também a palavra “LLM” para o serviço de inteligência artificial. “Modelo do plano” descreve quais campos a geração deve preencher; o rótulo de um campo no painel não muda sozinho o PDF.

Para uma alteração de conteúdo já combinada, edite o campo correspondente, salve, use “Montar o prompt” para conferir o que será enviado e depois faça o teste de geração. Não inclua contato no prompt por hábito. Campos marcados “Campo interno: fica fora da projeção participante, do PDF e da prévia” são de uso interno; essa restrição também se aplica aos campos dentro de um grupo ou lista internos. Confira a saída em “Ver como participante” ao revisar um pedido.

A pontuação tem pontos por opção, fórmulas, faixas, prioridade e persona. Use “Calcular a pontuação” com respostas fictícias para conferir o resultado. Prioridade e persona são de uso interno. A apresentação de notas no PDF depende das seleções do editor do PDF.

Listas e calendários em “Dados do formulário” dão informações de apoio à geração. Algumas partes usam “Valor (JSON)”; “O resto da definição (JSON)” e os campos de instruções também exigem estrutura correta. Peça à administradora técnica para alterar fórmulas, referências, calendários, JSON ou templates quando não souber como preservar a estrutura. Você pode conferir o resultado e aprovar o conteúdo sem escrever código.

**Se o teste ou salvamento recusar:** siga o caminho do erro, preserve a versão publicada e encaminhe a mensagem segura à administradora técnica. Não apague uma regra de validação só para permitir a publicação.
