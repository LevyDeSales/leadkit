# Acesso avançado: chaves e assistentes

[Sumário do guia](README.md) · [Integrações](integracoes.md)

Para o trabalho diário, use o login do painel. Esta seção serve para quando uma administradora técnica prepara acesso por um sistema ou assistente; não é necessário configurar essas ferramentas para criar e acompanhar formulários.

## Decidir o acesso de um sistema

“Avançado” (`/admin/advanced`) reúne “Chaves de API”, “MCP” e “Referência da API”. Abrir essa página apenas navega; não cria credenciais nem ativa conexões.

**Onde entrar:** “Avançado” → “Chaves de API” (`/admin/api-keys`). Uma chave de API é uma credencial para um programa agir em nome de um acesso autorizado.

1. Combine o nome do cliente e o trabalho permitido com a administradora técnica. “Leitura” permite consultar configuração, contagens e estados, sem dados da participante; “Total” permite as operações administrativas. Para exploração, prefira leitura.
2. Quando a criação estiver aprovada, preencha “Nome”, “Papel” e “Validade em dias” e clique em “Criar a chave”. O nome não muda e não pode ser reaproveitado, nem após revogação.
3. Copie o valor mostrado uma única vez para um cofre autorizado. Não o coloque em URL, captura de tela pública, repositório ou conversa.
4. A administradora configura o cliente e confere seu funcionamento. Você pode acompanhar “Situação” e “Último uso”. As ações ficam na auditoria com o nome da chave.

**Resultado:** um acesso de programa, com o alcance concedido. A chave não substitui por si só as confirmações e os estados exigidos para ações como liberação de plano.

**Se perder ou suspeitar de uso indevido:** revogue a chave correspondente e combine outra com a administradora. Revogar interrompe os clientes que a usam; não há recuperação do valor original. A validade pode expirar, e o último uso não comprova que o cliente executou corretamente toda sua tarefa.

## Consultar a referência da API (opcional)

**Onde entrar:** “Avançado” → “Referência da API” (`/admin/api/v1/reference`). Consulte-a com a responsável técnica se precisar entender uma integração. Sua sessão do painel permite abrir a documentação; abrir e ler não executa operações nem concede permissão de escrita pela API.

As chamadas exigem uma chave de API própria, com o papel exigido pela operação. O botão “Test Request” executa a operação na instalação real, com os efeitos descritos; não é uma simulação. Deixe a responsável técnica avaliar a operação e conduzir qualquer execução.

## Usar um assistente conectado

**Onde entrar:** “Avançado” → “MCP” (`/admin/mcp`). MCP é a conexão que permite a um assistente usar ferramentas administrativas. A página contém orientações técnicas para o cliente; instalação, transporte e configuração cabem à administradora técnica.

Defina antes o que o assistente pode consultar ou alterar. Conectar não autoriza publicar formulário, liberar plano, trocar credenciais ou reenviar dados por decisão própria. Use uma credencial com o menor alcance necessário e confira os resultados no painel.

Se o cliente informar recusa, cancelamento, resposta inválida ou falha de confirmação, a operação
fica bloqueada. Isso não comprova que você clicou ou que uma janela foi exibida. Peça à
administradora técnica para conferir a conexão antes de tentar novamente; não peça ao assistente
para ignorar a confirmação. Se a mensagem disser que o envio à API pode ter ocorrido, confira o
estado no painel antes de repetir. O [guia técnico de recuperação](../../app/README.md#confirmação-e-recuperação-do-mcp)
detalha a diferença.

Credenciais de encaminhamento, credenciais de fontes de eventos, segredo do Webhook e token do Baserow têm finalidades próprias. Uma chave administrativa não funciona como credencial do resolvedor de encaminhamento. Peça à administradora a credencial correta; não tente reutilizar o mesmo segredo em todas as áreas.

Referências técnicas opcionais para a equipe: [referência da aplicação](../../app/README.md), [contratos de integração](../api/integracoes.md) e [contrato da jornada](../api/jornada.md). Elas complementam este guia e não são tarefas de implementação para a operadora.
