# Contribuir com Leadkit

Use Python 3.11 e `uv sync --locked --extra dev --extra mcp`, com venv fora do repositório.
Documentação, interface, comentários e docstrings usam pt-BR; nomes de código e commits usam inglês.
Abra mudanças pequenas que expliquem o comportamento desejado e como ele foi verificado.

## Testes e lint

A suíte exige PostgreSQL 16 real. Crie um banco descartável próprio terminado em `_test`; as tabelas
são recriadas e esvaziadas. O reset confere a URL e o banco conectado antes do DDL, sem usar
downgrade nem desabilitar o guard de produção. Nunca execute a suíte contra dados de uma instalação.

```sh
export TEST_DATABASE_URL=postgresql+psycopg://leadkit@127.0.0.1:5432/leadkit_test
python -m pytest -q
ruff check app tests_app
ruff format --check app tests_app
```

No macOS, use `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`. Temporários e venv podem ficar fora
da árvore. Para paralelizar, `python -m pytest -q -n auto --dist loadgroup`; cada worker cria seu
banco `<nome>_gwN_test` e o usuário precisa de `CREATEDB`. O teste e2e de Hatchet exige serviço isolado
próprio; veja seus requisitos em `tests_app/test_e2e_hatchet.py`. Não adicione credenciais reais.

Fixtures públicas ficam em `tests_app/fixtures/forms/`. Os slugs técnicos estáveis dos cenários não
representam campanhas ou instalações reais. Ao trocar uma fixture, preserve os aceites de envio,
validação, condições, pontuação, calendário, versões, PDF, webhook e equivalência admin/API/MCP.

## Formulários

Definições em formato 1 são compiladas antes da publicação. Uma versão publicada é imutável;
altere um rascunho e publique uma nova versão. Preserve a compatibilidade das macros `brand.*`,
classes de templates e contratos de endpoints. Perguntas de contato não devem ir ao prompt.
Exemplos usam domínios reservados como `example.test` e textos fictícios próprios.

## Hatchet e dados da lead

Tasks recebem/devolvem somente IDs e contagens. Respostas, contatos, documentos, tokens e
mensagens de provedores não entram em logs, argumentos da fila ou exceções. Preserve idempotência,
timeouts, erros seguros e a separação entre falhas permanentes e transitórias.

## Banco e integração

Migrations devem preservar constraints, índices e gatilhos de imutabilidade. A baseline pública
é destinada a banco vazio. Bancos existentes exigem um roteiro de compatibilidade separado;
não use `stamp` para ocultar divergências. Valide ações nas interfaces admin, API e MCP quando
compartilharem o mesmo serviço.

Não faça deploy, publique formulários ou acione serviços externos como efeito de testes. Arquivos
privados, histórico operacional e assets de uma instalação não pertencem a esta distribuição.

As validações locais e os gates de publicação estão em [docs/ci.md](docs/ci.md).
