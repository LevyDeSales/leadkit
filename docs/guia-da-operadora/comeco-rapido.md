# Começo rápido por finalidade

[Sumário do guia](README.md) · [Criar e salvar](criar-e-salvar.md)

## Escolha o resultado esperado

A finalidade determina o que será produzido. A conclusão determina o que a participante verá depois de salvar as respostas. São escolhas separadas.

| Sua finalidade | “Geração” no rascunho | “Conclusão” possível |
| --- | --- | --- |
| Coletar respostas sem inteligência artificial ou arquivo | “Somente salvar respostas” | Confirmação local, botão ou encaminhamento |
| Produzir um plano e um PDF | “Gerar plano e PDF” | Acompanhar o resultado, confirmação local, botão ou encaminhamento |

“Captura” significa respostas salvas sem geração. Não escolha “Acompanhar o resultado do PDF (padrão)” para captura: a combinação é recusada.

## Preparar somente a coleta de respostas

**Onde entrar:** “Formulários” → “Criar um formulário”.

1. Preencha “Título” e confira “Endereço público”.
2. Em “Finalidade”, escolha “Somente salvar respostas, sem IA ou PDF” e clique em “Criar o rascunho”.
3. No rascunho, ajuste as perguntas e os consentimentos conforme [Configurar o formulário](configurar-formulario.md).
4. Em “Depois de salvar as respostas”, escolha a conclusão pela tabela abaixo.
5. Clique em “Salvar o rascunho”, faça a [validação local da captura](testar-e-publicar.md) e confira a conclusão salva.
6. Quando estiver pronta para abrir o formulário ao público, siga [Publicar](testar-e-publicar.md).

**Resultado:** após a publicação e um envio aceito, as respostas aparecem em “Respostas” → “Somente respostas”, na lista de capturas. Não espere PDF para esse envio.

**Se houver erro:** confira “Geração” e “Conclusão” juntas. Falha de integração não exige que a participante responda novamente.

## Preparar geração de plano e PDF

**Onde entrar:** “Formulários” → “Criar um formulário”.

1. Preencha os dados iniciais e escolha “Gerar plano e PDF” em “Finalidade”.
2. Clique em “Criar o rascunho”. Configure perguntas, instruções e modelo do plano com a responsável pelo conteúdo e, quando necessário, a administradora técnica.
3. Em “Liberação do plano (somente PDF)”, escolha “Automática” ou “Manual”. Manual exige uma escolha explícita e publicação; gerar o arquivo não libera o acesso.
4. Escolha como concluir o envio, salve, confira a aparência e [teste o rascunho](testar-e-publicar.md).
5. Revise os destinos de integração antes de publicar. Quando estiver pronta, publique a versão e confirme que o formulário está ativo.

**Resultado:** novos envios geram pedidos que você acompanha em “Respostas” → “Com PDF”. O PDF depende da geração e dos serviços configurados na instalação.

**Se houver erro:** confira o teste e peça ajuda técnica se a fila, a geração ou o armazenamento estiverem indisponíveis. Não use reprocessamento apenas para testar uma configuração externa.

## Escolher a tela depois do envio

**Onde entrar:** “Editar o rascunho” → “Depois de salvar as respostas” → “Conclusão”.

| Escolha da tela | Comportamento |
| --- | --- |
| “Acompanhar o resultado do PDF (padrão)” | Leva ao acompanhamento do plano. Só serve para finalidade PDF. |
| “Mostrar confirmação local” | Mostra um recibo com mensagem, sem destino externo. |
| “Mostrar botão para outro destino” | Mostra a mensagem e um botão; a participante decide quando continuar. |
| “Encaminhar para outro destino” | Mostra o recibo e tenta seguir ao destino automaticamente; o botão permite continuar se a navegação não acontecer. |

Para botão ou encaminhamento, preencha “Destino do botão ou encaminhamento” com o endereço HTTPS completo e “Rótulo do botão de continuidade” com o texto desejado. Não use usuário ou senha na URL. Nos modos locais, deixe o destino vazio. “Mensagem de confirmação” aceita texto puro até 2000 caracteres; o rótulo aceita até 300. Em branco, são usados os textos padrão da tela.

Clique em “Salvar o rascunho” e depois em “Conferir conclusão salva”. A prévia usa o que foi salvo, deixa o botão desativado e não abre o destino. Uma falha da página externa não desfaz respostas já salvas: se o recibo apareceu, oriente a continuar pelo botão sem repetir o formulário.

Essa configuração não envia respostas por webhook nem concede acesso ao PDF. Para continuar em um serviço que precise relacionar o envio, há a opção avançada de [referência de encaminhamento](encaminhamento-e-jornada.md).

Ao trocar um rascunho de PDF para captura e salvar, a configuração de geração é retirada desse rascunho. Se voltar a PDF, a configuração parte do modelo inicial; revise-a antes de publicar. As versões publicadas anteriores permanecem preservadas.
