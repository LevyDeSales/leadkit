# Tracker de trabalho de produto

Todo trabalho de produto do Leadkit, incluindo specs, features, bugs e tickets de implementação,
vive em [GitHub Issues de LevyDeSales/leadkit](https://github.com/LevyDeSales/leadkit/issues).
Use a CLI `gh` ou a integração GitHub disponível, conferindo o repositório antes de operar.

## Consumir e registrar trabalho

Antes de propor trabalho, leia a issue, comentários, dependências e decisões relacionadas.
Em `to-spec` e `to-tickets`, transformar contexto em spec ou tickets significa usar esse tracker
real: procure duplicatas e registre problema, resultado esperado, limites, aceites observáveis e
bloqueios. Relacione tickets à spec por links; dependências técnicas continuam válidas mesmo com
`ready-for-agent`. A label não inicia execução nem autoriza efeitos externos.

Uma instrução de skill para “buscar o ticket” significa ler a issue; “publicar no tracker”
significa escrever uma issue somente dentro da autorização da tarefa. Criação, comentário,
classificação e fechamento precisam estar no escopo autorizado; a skill não concede permissão.
Autorizações existentes na conversa permanecem válidas para esse escopo e não devem ser pedidas
novamente. Pedidos de criar ou organizar tickets já autorizam as escritas correspondentes no
GitHub. Decisões de setup confirmadas não exigem nova rodada de confirmação.
Para corpos multilinha via `gh`, prefira arquivo temporário e `--body-file`, após conferir o
`--help` da versão instalada.

Não mantenha backlog paralelo em `.scratch` nem specs locais concorrentes com as issues.
Documentação durável do domínio fica nos locais de [domain.md](domain.md); material privado,
relatórios operacionais e configurações de instalação ficam fora do repositório e das issues
públicas. Resumos públicos usam dados sintéticos e referências públicas seguras.

## Triagem e PRs

Use o vocabulário de [triage-labels.md](triage-labels.md). **PRs as a request surface: no.**
PRs são entregas vinculadas a issues, não uma fila paralela de solicitações. Uma skill que proponha
labels próprias deve primeiro verificar o vocabulário e o escopo autorizado, sem inventar
taxonomia. A coordenadora cria as labels canônicas ausentes como parte do setup já autorizado.
