# Skills compartilhadas e orquestração

## Fonte e disponibilidade

O conjunto aprovado contém 38 cópias oficiais de
[mattpocock/skills](https://github.com/mattpocock/skills), fixadas no commit
[`5beb35b040e3de97e54fa9e386d7e42d15c295c2`](https://github.com/mattpocock/skills/tree/5beb35b040e3de97e54fa9e386d7e42d15c295c2).
O destino compartilhado é `.agents/skills` no HOME de quem usa Codex (`~/.agents/skills`), compatível com
as contas Codex usadas no Orca. A instalação global é separada deste setup documental por repo:
esta árvore não instala, atualiza nem comprova o estado de cada conta.

Em outra máquina, use a skill local `skill-installer` como meio de instalação e confira origem,
SHA e inventário das 38 skills antes de usar. Consulte também a
[documentação oficial de skills](https://learn.chatgpt.com/docs/build-skills).
Mantenha as cópias compartilhadas fora do repositório; não versione symlinks absolutos nem copie
configuração `.claude` privada. Atualização de versão exige autorização própria.
Disponibilidade das 38 skills não garante que recursos específicos de outro runtime funcionem:
adapte apenas o que for compatível com Codex/Orca, sem lançar Claude.

## Escolher pelo problema

Leia a skill pertinente antes de agir; use somente as necessárias ao escopo, sem ritual de rodar
todas nem refatoração de arquitetura automática.

Algumas skills upstream exigem invocação explícita e podem não aparecer no catálogo automático.
A coordenadora seleciona a pertinente e a nomeia explicitamente no prompt da Task, por exemplo
`$to-tickets`, indicando o caminho compartilhado se necessário. Levy não precisa memorizar nomes.
A disponibilidade de uma skill não concede autoridade de execução; preserve o escopo autorizado,
sem modificar upstream nem habilitar hooks, scripts ou Claude.

| Situação | Skills e resultado esperado |
| --- | --- |
| Decisão de produto genuinamente aberta | `grill-with-docs` / `grilling`: investigar documentos e código primeiro, depois formular perguntas claras sobre as alternativas ainda abertas. Não reabrir decisões tomadas nem transferir investigação técnica a quem solicita. |
| Contexto pronto para especificação ou decomposição | `to-spec` / `to-tickets`: produzir spec e tickets no [tracker real](issue-tracker.md), com aceites e dependências. |
| Relato de comportamento inesperado | `triage`: reunir fatos antes de chamar de bug; seguir o [vocabulário público](triage-labels.md). |
| Mudança delimitada ou spec aprovada | `implement` / `implement-spec`, conforme o escopo; preservar contratos e validar os comportamentos principais. |
| Entrega pronta para avaliação | `code-review`: revisão independente da implementação, com achados e evidências para a coordenadora. |
| Quem solicita não entendeu a explicação ou o rumo | `wait-what`: explicar em linguagem clara e restabelecer o entendimento antes de continuar a parte dependente. |

Priorize UX/UI e regras de negócio. Teste comportamentos principais e checks existentes;
não imponha metas de cobertura, mutação, complexidade, tamanho ou quantidade de dependências.
Skills orientam o método: a instrução de quem solicita e o escopo autorizado governam a ação.

Para converter padrões pesquisados em aceites de UX, editor, projeções e recuperação, consulte
[product-quality.md](product-quality.md) na especificação, implementação e revisão da mudança.

## Papéis no Orca

O mapeamento é por função, sem promessa de equivalência de desempenho entre modelos.

| Função | Modelo | Esforço |
| --- | --- | --- |
| Coordenadora: escopo, delegação, síntese e decisões | `gpt-6-astra` | `ultra` |
| Implementadora de mudança delimitada | `gpt-6.1-sol` | `medium` |
| Revisora independente de alterações | `gpt-6.1-sol` | `high` |
| Gestora de issues | `gpt-6.1-sol` | `medium` |
| Guardiã da API: revisão de admin/API/MCP e serviços compartilhados | `gpt-6.1-sol` | `high` |
| [Guardião da qualidade](guardiao-da-qualidade.md): revisão dos cinco eixos de qualidade | `gpt-6.1-sol` | `high` |
| Operadora: execução de roteiro e tarefas mecânicas | `gpt-6-luna` | `medium` |

Use exclusivamente Orca supervisionado, com no máximo três workers além da coordenadora;
não use spawn nativo nem coordenação paralela. Carregue as skills atuais `orca-cli` e
`orchestration` da instalação; consulte o `--help` antes de opções desconhecidas. Se a versão
instalada não oferecer o recurso, registre o bloqueio sem improvisar outra orquestração.

Cada objetivo tem um Run, cada resultado uma Task e cada tentativa um Dispatch. A Task explicita
alvo, resultado, ownership, restrições, dependências, aceite observável e skills concretas.
Nomeie `implement`/`implement-spec` para a autora e `tdd` quando houver mudança de comportamento;
em instruções/documentação, use `writing-for-agents` e `research` quando houver fontes a investigar.
Nomeie `code-review` para a revisão independente e os papéis de guardiã nos casos abaixo.
Use só as workers necessárias, com ownership sem disputa; código isolado parte de `origin/develop`, preservando o
worktree principal em `main`. Confira modelo e esforço efetivos no recibo antes de afirmar seleção.

Workers seguem o preâmbulo ativo para mensagens, perguntas bloqueantes, heartbeat e entrega.
A coordenadora valida resultado e evidências da Task/Dispatch ativos e decide reutilizar, reter
por pedido de quem solicita ou liberar o terminal antes do reconhecimento da entrega.
Silêncio não prova encerramento; recuperação segue a skill atual. Ao terminar, todas as workers
precisam de destino explícito e os terminais devem ser liberados pelo ciclo Orca após o aceite.

A revisora é independente da implementadora. Revisão sem escrita não transfere ownership;
correções retornam por novo Dispatch. Mudança em `app/admin/`, `app/admin_mcp/`, `app/api/` ou
serviços usados pelo admin exige guardiã da API, incluindo equivalência admin/API/MCP, segurança e LGPD.
Se a guardiã implementar correções, outra agente revisa essa implementação.

Todo PR com código, testes, dependências, CI ou estrutura de módulos exige também o
[guardião da qualidade](guardiao-da-qualidade.md), independente da autora, com parecer sobre
cobertura, dependências, complexidade ciclomática, tamanho/responsabilidades e mutações.
Cada eixo informa verificado, não aplicável com razão ou bloqueador. Documentação isolada
valida texto, links e diff proporcionalmente, sem pytest ou mutações inúteis.
A revisão da API/MCP permanece obrigatória em paralelo nos seus caminhos, em Tasks distintas
respeitando o limite de três workers além da coordenadora. O papel permanente tem perfil e gatilho
estáveis, sessões por Task e encerramento/liberação normais; não é daemon ocioso.
A tabela seleciona modelo/esforço; preserve `codex` como nome de runtime.

## Integração e autorização

Features e correções vão por PR para `develop`, com todos os checks aplicáveis concluídos e
verdes no mesmo commit, incluindo `Tests / app-tests` e `ci-ready`. Consulte
[docs/ci.md](../ci.md) e os workflows atuais: documentação isolada dispensa pytest local,
mas não dispensa os gates remotos existentes.

Promoção `develop` → `main` usa merge commit, sem apagar `develop`. Deploy em produção exige commit de
`main`, Release correspondente (`vYYYY.MM.DD.x`, data de São Paulo; tags legadas `vAAAA.M.N` seguem
válidas) e autorização explícita de Levy na conversa atual.
Homologação/deploy de `develop` em DEV segue o escopo autorizado, checks verdes e alvo isolado;
a exigência de `main` e Release acima é de produção. Autorização já concedida para DEV permanece
válida no alvo e escopo concedidos, sem nova confirmação; não se estende à produção.
O publisher permanece OFF; este setup não ativa variáveis, bootstrap, automações ou configuração
externa. A existência de um plano de release não comprova publicação nem permite deploy.

A autorização existente na conversa permanece válida para o escopo concedido: prossiga sem
pedi-la novamente. Pedidos de criar ou organizar tickets autorizam as escritas correspondentes
no GitHub; decisões de setup confirmadas dispensam nova rodada de confirmação.

Escritas em serviços externos precisam estar no escopo autorizado. Deploy, publicação de
formulário ou página, mensagens externas e ativação de automação continuam exigindo ok explícito
de Levy para essas ações. Billing liberado ou comandos sugeridos por skills não o substituem.
Formulários e conteúdo cabem à operadora; código e segurança cabem à equipe, conforme
[domain.md](domain.md). Use dados sintéticos nos ensaios e mantenha segredos e material de
instalação fora do chat, logs, argumentos de comandos e repositório público. Carregue segredos
somente no ambiente, a partir de arquivo com modo `600`; preserve `WEBHOOK_ENCRYPTION_KEY`,
sem regenerá-la.
