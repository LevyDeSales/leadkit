# Instruções para agentes — Leadkit

A instrução atual da usuária prevalece sobre estes documentos e sobre skills. Fale com a usuária
no feminino e escreva interface e documentação em pt-BR. Commits: `tipo: assunto` em inglês,
no imperativo, com o motivo no corpo.

- **Produto ou testes:** leia [CONTRIBUTING.md](CONTRIBUTING.md). Preserve contratos HTTP/MCP,
  versões imutáveis de formulário, macros de templates e identificadores persistidos.
- **Exploração do domínio:** leia [domain.md](docs/agents/domain.md), o glossário e as ADRs pertinentes.
- **Specs, features ou tickets:** use [GitHub Issues](docs/agents/issue-tracker.md) para todo trabalho
  de produto; material privado fica fora do repositório.
- **Triagem:** consulte [triage-labels.md](docs/agents/triage-labels.md) antes de classificar uma issue.
- **Escolha de skills, delegação, revisão ou integração:** siga
  [skills-orchestration.md](docs/agents/skills-orchestration.md), inclusive os limites de autorização.
- **Validação e release:** consulte [docs/ci.md](docs/ci.md). Testes usam Python 3.11, dados sintéticos
  e banco próprio terminado em `_test`; a suíte apaga tabelas. Documentação isolada exige validar
  links e diff, sem pytest local. Nenhuma credencial, asset ou dado de instalação entra em exemplos.
