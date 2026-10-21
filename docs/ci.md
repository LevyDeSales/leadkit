# Validação e publicação

A CI separa testes do app, pacote instalado e validação de imagem/Swarm. Os ensaios usam banco e
serviços descartáveis, formulários fictícios importados explicitamente e configurações derivadas
dos exemplos distribuídos. A execução de validação não é deploy de uma instalação.

`Tests / app-tests` executa a suíte em Python 3.11 e PostgreSQL 16, lint, contrato OpenAPI e prova do
wheel instalado fora do checkout. O fluxo de packaging constrói a imagem, renderiza os exemplos e
executa os ensaios aplicáveis de Compose e duas stacks Swarm, incluindo atualização, rollback,
backup e isolamento de nomes. A agregação `ci-ready` exige que as validações do mesmo commit tenham
passado; etapas obrigatórias não são substituídas por uma execução antiga de outro SHA.

A imagem confere os hashes de Python, templates e assets da cópia de fontes e do pacote importado
com `python -I`, sem rede. Arquivos ausentes, extras ou diferentes reprovam o gate. O build força
a reconstrução apenas do projeto local, conservando o cache das dependências: mudar só um template
precisa atualizar o wheel mesmo quando o metadado do projeto não muda. Isso evita que o cache quente
instale um pacote antigo ao lado de fontes novas.

Os nomes técnicos não precisam coincidir com os modelos públicos: `STACK` ausente/vazia conserva
`gerador-planos` no YAML/scripts, enquanto `STACK_NAME=leadkit` no exemplo é uma escolha explícita.
O prefixo `mcc-` dos arquivos de backup permanece compatível com `backup.sh`. Usuário e banco de
Postgres do ensaio são derivados do modelo correspondente; a segunda stack recebe nome, domínio e
host próprios sem modificar a primeira. A verificação de nomes compara essas configurações
separadamente e não pressupõe que o default do YAML seja o nome do exemplo.

## Release

O publisher fica desligado por padrão. Ele exige a opção de repositório
`LEADKIT_PUBLISH_ENABLED=true`, push da branch `main` no repositório autorizado (sem fork) e todos
os gates do mesmo commit verdes. A primeira versão sem tags exige também o opt-in explícito de
bootstrap; uma lista remota vazia não é tratada automaticamente como autorização de release.
`workflow_dispatch` planeja a versão sem publicar. Nenhuma dessas opções deve ser ativada por um
ensaio de desenvolvimento ou por esta preparação pública.

Uma publicação futura exige revisão e autorização da mantenedora, inclusive configurações de
imagem, release e infraestrutura. Não copie tags/histórico privados para contornar o bootstrap e
não considere execução local equivalente a um check remoto verde.

## Reproduzir localmente

Use venv externo, PostgreSQL 16 descartável e `TEST_DATABASE_URL` terminado em `_test`:

```sh
python -m pytest -q -n auto --dist loadgroup
ruff check app tests_app
ruff format --check app tests_app
python -m app.admin.api.openapi --check
uv build --wheel --out-dir /caminho/privado/wheels
```

Instale o wheel sem modo editável num ambiente próprio, com as dependências do lock, e execute
`python -I /caminho/do/checkout/tests_app/test_packaging.py --installed` a partir de outro diretório.
Assim o pacote precisa fornecer seus próprios assets, templates, fontes e migrations. O teste de
CLI MCP usa uma configuração deliberadamente incompleta, sem conexão externa; os PDFs usam o
renderizador real. Testes com Hatchet de verdade exigem serviço isolado e configuração própria.

Quando Docker ou serviços externos não estiverem disponíveis, registre essa limitação e execute
os ensaios locais aplicáveis. O stub do renderer nos testes de configuração confere o shell e o
YAML reais, mas não substitui `docker stack config`, construção de imagem ou Swarm real.
