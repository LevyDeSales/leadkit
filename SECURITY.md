# Segurança

Relate vulnerabilidades de forma privada pelo [recurso de relato de segurança do GitHub](https://github.com/LevyDeSales/leadkit/security/advisories/new),
quando disponível, ou pelo canal privado indicado pela mantenedora. Evite abrir uma issue pública
com detalhes exploráveis antes de combinar a divulgação.

Inclua a versão afetada, passos mínimos com dados fictícios, impacto observado e uma sugestão de
reprodução. Não envie tokens, senhas, respostas de participantes, PDFs reais ou cópias de banco.

Mantenha dependências atualizadas, use HTTPS e segredos próprios, limite o acesso ao admin e
configure retenção adequada à sua instalação. Templates e assets devem respeitar o isolamento do
renderizador; o overlay de marca deve ser montado somente leitura e fora do repositório.

## MCP de admin

Use papel `leitura` para consultas e aprovação por chamada no cliente para controlar escritas.
O fluxo de duas etapas de clientes sem formulário é uma convenção com o assistente: ele pode fazer
as duas chamadas seguidas. Não substitui a aprovação do cliente nem o papel da chave. Com formulário,
somente `accept` com `confirmar: true` booleano autoriza; recusa, cancelamento, resposta inválida e
falha técnica bloqueiam e invalidam a pendência da chamada inteira. Não desative essa proteção
para contornar uma janela ausente. Veja [confirmação e recuperação](app/README.md#confirmação-e-recuperação-do-mcp).

Compartilhe apenas os metadados permitidos de diagnóstico, sem perguntas, argumentos, arquivos,
respostas pessoais, identificação bruta do cliente ou exceções completas. Uma resposta `decline`
comprova somente a resposta do cliente; não demonstra recusa humana ou exibição de uma janela.
