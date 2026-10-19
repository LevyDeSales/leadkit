# Instruções para agentes — Leadkit

Código e testes usam Python 3.11. Preserve contratos HTTP/MCP, versões imutáveis de formulário,
macros de templates e identificadores persistidos. A interface e a documentação são em pt-BR.

Leia [CONTRIBUTING.md](CONTRIBUTING.md) antes de alterar o produto. Teste apenas com dados fictícios
em banco próprio terminado em `_test`; a suíte apaga tabelas. Não use credenciais, assets ou dados
de uma instalação em exemplos. A marca privada fica fora desta árvore.

Execute `ruff check app tests_app` e testes adequados ao comportamento alterado. Commits usam
`tipo: assunto` em inglês. Publicação, deploy e escrita em serviços externos exigem autorização
explícita de quem mantém a instalação.
