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

## Logo na abertura do formulário

Em “Editar o rascunho” → “Página pública”, escolha uma das imagens em “Imagem do logo de abertura”
e preencha “Texto alternativo do logo” com a identidade que ela apresenta. Use “Enviar ou gerenciar
imagens” se ainda precisar enviar um arquivo. A lista mostra as imagens disponíveis para esse
formulário, com o nome e o escopo.
O texto alternativo é obrigatório quando houver logo. Imagens de outro formulário e URLs remotas
não são aceitas; a biblioteca geral e a prioridade da imagem do próprio formulário seguem as regras
acima. Para retirar o logo, escolha “Sem logo” e salve; não é necessário apagar o texto alternativo.
Isso também funciona sem JavaScript. Na API/JSON/MCP, a referência continua sendo `asset:nome` no
campo `opening_logo.src`, com `opening_logo.alt` obrigatório; remover `opening_logo` retira a configuração.

O logo aparece acima do título, preservando a proporção no celular e no computador. Funciona também
quando o formulário somente salva respostas. A marca global e o logo do PDF têm configurações
independentes. Sem logo configurado, a abertura continua com o título e a introdução.

Salve e confira “Ver a página no celular”. A prévia usa o arquivo atual; a página pública usa o
conteúdo fixado na versão publicada. Substituir a imagem marca uma mudança pendente e exige nova
publicação para chegar à página pública. Uma versão anterior continua com sua imagem e seu texto
alternativo, mesmo depois da substituição. Uma imagem usada por versão publicada permanece
protegida contra remoção. Se o arquivo estiver indisponível, o navegador apresenta o texto
alternativo; confira o armazenamento com a administradora técnica.

**Compatibilidade de instalação:** antes de configurar `opening_logo`, a administradora técnica
precisa atualizar todas as instâncias que leem formulários para uma versão que reconheça esse
campo. Definições antigas sem logo continuam válidas e salvar pelo editor não adiciona um logo
vazio. Imagens do aplicativo anteriores ao suporte ao campo recusam a nova definição. Depois de
publicar uma versão com logo, um retorno deve usar uma imagem do aplicativo que também reconheça
o campo; versões publicadas são imutáveis. Não remova campos de uma versão histórica para tentar
compatibilizá-la com um aplicativo antigo.
