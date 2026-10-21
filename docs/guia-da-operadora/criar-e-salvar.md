# Entrar, criar e salvar

[Sumário do guia](README.md) · [Próximo: configurar](configurar-formulario.md)

## Entrar no painel

**Objetivo:** acessar o ambiente correto para sua operação.

1. Abra o endereço do painel fornecido pela administradora técnica, com o caminho `/admin`.
2. Em “Entrar no painel”, preencha “Usuário” e “Senha” e clique em “Entrar”.
3. Confira a identificação do ambiente, quando exibida, e seu usuário no topo. A versão do sistema aparece no rodapé.

**Resultado:** quando você entra sem um destino específico, o painel abre “Formulários”. O menu oferece “Formulários”, “Respostas”, “Imagens”, “Integrações”, “Avançado” e “Guia de uso”. O nome da aplicação no topo também leva a “Formulários”. Se você abriu uma tela protegida antes de entrar, o login pode retornar a ela.

“Guia de uso” abre o guia público de `develop` no GitHub; ele pode estar à frente da versão instalada. Para encerrar, clique em “Sair”.

**Se não entrar:** confira suas credenciais. Tentativas repetidas podem causar bloqueio temporário; respeite a espera informada. Não há recuperação de senha por e-mail nessa tela: peça ajuda à administradora técnica. Se a sessão expirar durante uma edição, entre novamente e confira o que ficou salvo antes de repetir a ação.

## Criar ou copiar um formulário

**Onde entrar:** “Formulários” (`/admin/forms`) → “Criar um formulário”.

1. Digite o “Título”.
2. Confira “Endereço público”: é a parte final do link, com letras minúsculas sem acento, números e hífens. Em branco, ela é criada a partir do título.
3. Em “Começar de”, mantenha o modelo inicial ou escolha uma cópia de um formulário existente.
4. Em “Finalidade”, preserve a origem ou escolha somente respostas ou geração de PDF.
5. Clique em “Criar o rascunho”.

**Resultado:** um formulário ainda sem publicação. O endereço pode mudar até a primeira publicação; depois fica fixo. Copiar usa a versão publicada da origem, ou seu rascunho quando ela ainda não foi publicada. A política de liberação é conservada. O vínculo de encaminhamento e suas credenciais não são copiados: revise o receptor próprio.

**Se houver erro:** corrija o campo indicado, inclusive endereço já utilizado. Não trate um rascunho criado como um formulário já disponível ao público.

## Salvar uma edição

**Onde entrar:** abra o formulário e clique em “Editar o rascunho”.

1. Faça as alterações necessárias. “Adicionar”, “Remover”, “Subir” e “Descer” organizam o que está na tela, mas ainda exigem salvamento.
2. Clique em “Salvar o rascunho”. O sistema confere a definição inteira.
3. Confira a indicação de salvamento e as mudanças pendentes antes de sair da página ou abrir uma prévia.

**Resultado:** o rascunho aceito fica disponível para teste e publicação. Não há promessa de salvamento automático; mudar a tela não comprova que foi salvo. A versão pública continua igual.

**Se aparecer “O rascunho não foi salvo”:** abra cada link do resumo de problemas, corrija e salve de novo. Se houver aviso de alteração por outra aba ou pessoa, preserve sua proposta em local autorizado, reabra o rascunho atual e compare antes de reaplicar. Não sobrescreva sem conferir.

## Usar um arquivo de formulário existente

“Importar um formulário” não cria uma etapa de revisão em rascunho: a ação “Conferir e publicar a versão 1” valida o arquivo e publica diretamente. Use somente quando a publicação estiver decidida e a definição tiver sido revisada com a administradora técnica.

Nessa tela, você pode colar “Definição em JSON” ou selecionar “Ou o arquivo da definição”. Se escolher arquivo, ele prevalece sobre o texto colado. Confira o endereço dentro da definição, a “Nota da versão” e os destinos herdados antes de enviar. Se a validação recusar, corrija os problemas; não presuma publicação.

Para revisão sem ir ao ar, prefira criar ou copiar um rascunho. “Exportar a definição (JSON)” baixa a definição publicada para preservação ou trabalho com a equipe; não é o PDF da participante nem a exportação de respostas.
