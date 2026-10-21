# Imagens e aparência do PDF

[Sumário do guia](README.md) · [Testar e publicar](testar-e-publicar.md)

## Enviar ou substituir uma imagem

**Onde entrar:** “Imagens” (`/admin/assets`).

1. Em “Onde ela vale”, escolha a biblioteca geral ou um formulário específico. Uma imagem do formulário prevalece sobre a da biblioteca com o mesmo nome.
2. Preencha “Nome” conforme a referência que o template usará e selecione “Arquivo”. São aceitos PNG, JPEG ou WebP até 8 MB; SVG e GIF não são aceitos.
3. Clique em “Enviar e otimizar”. O sistema confere o conteúdo, retira metadados e reduz o lado maior a até 2000 pixels.
4. Para substituir uma existente, abra seu nome, selecione “Arquivo novo” e clique em “Trocar e otimizar”.
5. Confira a imagem e a seção “Quem usa”. Abra a prévia do formulário afetado e publique uma nova versão quando a troca estiver aprovada.

**Resultado:** a imagem fica disponível para os templates que a chamam pelo nome. Enviar uma imagem não a insere automaticamente no documento. A administradora técnica pode ajustar essa referência no template.

Versões publicadas fixam a imagem usada: trocar o arquivo não muda os pedidos antigos. Os envios novos só usam a troca após nova publicação do formulário.

**Se houver erro:** confira formato, tamanho e nome. A mensagem informa o problema. Uma imagem usada por versão publicada não pode ser removida. Remover outra imagem exclui seu arquivo e pode impedir salvar ou publicar rascunhos que ainda a referenciam; ajuste esses templates antes de confirmar “Remover a imagem”.

## Conferir e salvar a aparência

**Onde entrar:** rascunho → “Editar o PDF e a prévia”. Esta tarefa se aplica a formulários com geração de PDF.

1. Em “Prévias”, escolha “Plano de exemplo” ou, quando disponível, “Plano do último teste”. O exemplo não comprova a qualidade de uma geração real.
2. Clique em “Ver o PDF” e confira páginas, cortes e imagens. Use “Ver a página de resultado” para conferir o resumo que a participante verá.
3. Em “Notas que o PDF e a prévia podem mostrar”, selecione apenas as notas que devem chegar ao documento e ao resumo. Prioridade e persona ficam fora dessas saídas.
4. Se a aparência precisar de ajustes, peça à administradora técnica para editar “PDF (HTML)”, “PDF (CSS)” e “Prévia da página de resultado”. São campos de templates e estilos, não um editor visual de arrastar elementos.
5. Clique em “Salvar o PDF e a prévia” e confira o resultado. Depois siga a revisão e publicação do formulário.

**Resultado:** as alterações aceitas entram no rascunho. Os botões de prévia mostram os campos dessa página, inclusive alterações ainda não salvas, sem gravá-las. Isso difere da prévia das perguntas e da conclusão, que usam o rascunho salvo.

**Se houver erro:** confira o resumo com campo e linha indicados. Se só a prévia funcionou, ainda é necessário salvar. Se o erro for de template, imagem referenciada ou geração do arquivo, peça ajuda técnica antes de publicar. Nenhuma dessas prévias faz uma nova chamada de geração de texto.
