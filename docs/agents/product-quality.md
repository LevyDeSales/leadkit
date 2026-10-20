# Qualidade de produto: do padrão ao aceite

Use este guia ao especificar, implementar ou revisar UX, editor, projeções e recuperação.
Escolha os critérios do comportamento alterado e registre cenário, resultado observável e prova
no ticket existente. As referências dos onze projetos estudados são base prática de desenvolvimento;
os aceites abaixo são adaptações ao Leadkit, sem instalar ou copiar frameworks.

## Critérios verificáveis

- **UX clara e acessível:** ações e mensagens distinguem validar, salvar, gerar e liberar. Campos
  têm label, descrição/erro associados, resumo navegável e foco coerente; teclado alcança as ações
  afetadas. Verifique teclado e anúncio dos estados/erros com leitor de tela nesse fluxo, incluindo
  fallback sem JS quando aplicável. Um 422 leva ao erro corrigível e preserva os valores permitidos.
- **Confirmação após commit:** “salvo”, botão de destino e redirect dependem do aceite persistido.
  Falha de transação mantém recuperação; resposta perdida permite replay com a mesma chave.
  Duplo clique não cria outro pedido; falha de trigger após commit não pede reenvio das respostas.
- **Recuperação e idempotência:** distinga submissão persistida, tentativa, artefato atual,
  autorização, notificação e recebimento. Teste duplicata, conflito e falha transitória pertinentes;
  retry de entrega não gera plano novo. Nova tentativa da etapa de geração segue a política
  existente, sem promessa universal de exatamente uma chamada paga. Recebimento exige evidência própria.
- **Autorização no servidor:** a mesma operação em admin/API/MCP usa o serviço comum, papéis,
  confirmação e versão esperada aplicáveis. Botão oculto não autoriza leitura ou liberação;
  uma chamada direta sem permissão também é recusada.
- **Projeções por audiência:** filtre antes de serializar. Um marcador sintético interno em
  objeto/lista fica ausente de PDF, prévia, HTML inclusive oculto, JSON e integrações destinadas
  à participante; a projeção interna segue disponível à integração explicitamente autorizada.
  Registre as saídas afetadas e cobertas; logs, erros e Hatchet preservam a política de dados.
- **Compatibilidade:** preserve FastAPI, Jinja/JS, PostgreSQL e Hatchet, formulário por versão,
  versões publicadas imutáveis, compilador, macros e identificadores. Migrações são aditivas:
  código anterior continua operando com o banco migrado; publique nova versão para nova definição.
- **Prova proporcional:** teste comportamentos principais com exemplos sintéticos e execute os
  checks existentes aplicáveis em [ci.md](../ci.md). O ticket/PR informa cenário, evidência e limite;
  documentação isolada valida links e diff, sem pytest. Aplique ao delta, sem auditoria universal,
  metas de cobertura, mutação, complexidade, tamanho ou dependências.

## Aplicação aos tickets existentes

Leia o contrato corrente no tracker antes de trabalhar. “Pronto para aplicar” identifica o padrão
utilizável no core; não afirma que o ticket esteja implementado, integrado ou homologado.

| Ticket | Pronto para aplicar e preservar | Exemplo de aceite do delta / limite futuro |
| --- | --- | --- |
| [#7 — Conclusão após salvar](https://github.com/LevyDeSales/leadkit/issues/7) | Ciclo de envio FormKit/RHF; reaproveitar idempotência, fallback e erros existentes. | Com/sem JS, redirect só após commit; se a navegação automática não ocorrer, recibo e botão de destino permitem continuar sem reenviar. O aceite não pressupõe detectar indisponibilidade nem controlar a página de erro de outro domínio. Commit recusado não mostra sucesso. Editor completo/autosave novo não é pré-requisito. |
| [#8 — Captura sem PDF](https://github.com/LevyDeSales/leadkit/issues/8) | Dois eixos de configuração com erro na seção correspondente; serviço comum valida a combinação. | Captura aceita e replay não criam Job/LLM/PDF; “captura + resultado PDF” é recusado no servidor. Integrações aplicáveis e ativas recebem evento de submissão persistida: intenção na mesma transação, outbox idempotente e dispatch assíncrono após commit, sem payload PDF fictício. Rollback não envia; falha/timeout do provedor não bloqueia conclusão. Conclusão não anuncia geração inexistente. |
| [#11 — PDF da tentativa atual](https://github.com/LevyDeSales/leadkit/issues/11) | Separar processamento de disponibilidade e de permissão. | PDF ausente/purgado/inacessível ou de tentativa anterior não aparece disponível; completed histórico permanece. Consulta de disponibilidade, inclusive diante de falha de storage, não dispara nova chamada paga. Painel auxiliar é opcional. |
| [#12 — Projeções internas e participante](https://github.com/LevyDeSales/leadkit/issues/12) | Payload/Reflex orientam filtro antes da saída; preservar o contrato interno autorizado. | Marcador interno aninhado ausente de todas as representações à participante, inclusive HTML oculto; webhook interno autorizado mantém sua projeção. Documentar a lacuna concreta, sem presumir vazamento atual. |
| [#13 — Liberação por tentativa](https://github.com/LevyDeSales/leadkit/issues/13) | Consumir disponibilidade #11 e projeções #12 no serviço de autorização. | Chamada direta sem papel/confirmação ou com versão/tentativa divergente falha; repetição concorrente persiste um fato/intenção de notificar, sem colisão com conclusão. Reprocessamento permanece fechado. |
| [#14 — Eventos externos](https://github.com/LevyDeSales/leadkit/issues/14) | Contratos sintéticos de fonte, correlação, duplicata, conflito e ordem; estados distintos. | Mesmo event_id com conteúdo divergente conflita; evento tardio não recria dado excluído; HTTP 200 não prova leitura. Escopo integral futuro depende dos contratos #9/#10 e captura #8; painel não resolve essa dependência. |

## Referências primárias por assunto

Os caminhos nos links estão fixados aos commits do estudo de 19/10/2026. A coluna de aplicação
é orientação para o Leadkit, não garantia oferecida automaticamente pelo projeto de origem.

| Assunto | Fonte observada | Aplicação e fronteira |
| --- | --- | --- |
| Envio e erros | FormKit [forms.ts][FK-submit]; RHF [createFormControl.ts][RH-control]. | Validação pendente, envio, restauração e foco/erro; sucesso de callback não prova commit. |
| Editor, prévia e teclado | Filament [HasState.php][FI-state]; brijr [use-form-builder.ts][BR-builder] e [form.tsx][BR-form]. | Separar visibilidade/serialização e edição/prévia/JSON; exigir alternativa de teclado a reordenar. O hook brijr não prova persistência/publicação nem esse aceite de teclado. |
| Rascunho, publicação e projeções | Payload [Autosave/index.tsx][PA-auto] e [afterRead/promise.ts][PA-read]; Reflex [base_vars.md][RX-vars]. | Edição local, salvo e publicado são estados distintos; dado não renderizado ainda pode ter sido enviado à cliente. Preservar publicação explícita e filtro no servidor. |
| Auxiliares condicionais | NiceGUI [validation_element.py][NG-validation]; Dash [dash.py][DA-app]; Streamlit [form.py][ST-form]. | Referências de validação, painel e envio em lote; só avaliar ferramenta auxiliar se uma lacuna observada justificar. Consumir projeções autorizadas; reconexão/reload consulta o serviço, sem nova geração. |
| Referências pontuais | PyWebIO [input.py][PW-input]; Panel [widgets/input.py][PN-input] e [pyproject.toml][PN-manifest]. | Erro de grupo ligado ao campo e texto editado separado de valor aceito. A revisão Panel exige Python >=3.12; o core permanece 3.11. |

Autosave administrativo e editor ampliado são incrementos futuros quando houver problema observado:
resposta lenta não reverte edição local; token obsoleto indica conflito; texto inválido pode ficar
na tela, enquanto rascunho canônico continua compilável. Painéis NiceGUI/Dash/Streamlit não entram
na dependência crítica dos tickets nem substituem Hatchet ou o registro durável do servidor.

Estudo estático não prova desempenho, segurança integral ou conformidade integral de acessibilidade.
Quando houver incorporação real de código, asset ou dependência, confira a versão escolhida,
compatibilidade com o lock/runtime e licenças/avisos aplicáveis, inclusive extras. Leitura de padrões
não exige instalar projetos ou auditar toda a cadeia. Exemplos usam dados fictícios e destinos
como `https://example.test/conclusao`, sem dados, marcas ou hosts de instalação.

Para autoria, revisão independente, guardiã e skills concretas por Task, siga
[skills-orchestration.md](skills-orchestration.md). Investigue fatos do código antes de perguntar;
`grill-with-docs`/`grilling` serve a decisão real de produto ainda aberta. Autorizações já concedidas
continuam válidas dentro de seu escopo.

[FK-submit]: https://github.com/formkit/formkit/blob/411222eefaef67fb75abc1216108b2c8a6216033/packages/inputs/src/features/forms.ts
[RH-control]: https://github.com/react-hook-form/react-hook-form/blob/a88c81ae2b4becd2d5b79af9924b2e2b82f8d9b4/src/logic/createFormControl.ts
[FI-state]: https://github.com/filamentphp/filament/blob/c3cc62881eb32709d2a51b706f8846a4c51cc218/packages/schemas/src/Components/Concerns/HasState.php
[BR-builder]: https://github.com/brijr/forms/blob/22c32739c1cb0e82a66fdf60ff976d492a21fd98/registry/form-builder/use-form-builder.ts
[BR-form]: https://github.com/brijr/forms/blob/22c32739c1cb0e82a66fdf60ff976d492a21fd98/components/ui/form.tsx
[PA-auto]: https://github.com/payloadcms/payload/blob/c2c9143de8b01eb2d663529b6772e885de9de0d5/packages/ui/src/elements/Autosave/index.tsx
[PA-read]: https://github.com/payloadcms/payload/blob/c2c9143de8b01eb2d663529b6772e885de9de0d5/packages/payload/src/fields/hooks/afterRead/promise.ts
[RX-vars]: https://github.com/reflex-dev/reflex/blob/937c5a285c2d9a5c0844122b76fe2d3d09923ebf/docs/vars/base_vars.md
[NG-validation]: https://github.com/zauberzeug/nicegui/blob/44896cfcf8e6be63ee5644f9d63f399906abe6b7/nicegui/elements/mixins/validation_element.py
[DA-app]: https://github.com/plotly/dash/blob/c388ddf2f621496a23332052b50ebaeb4f5f50d6/dash/dash.py
[ST-form]: https://github.com/streamlit/streamlit/blob/5e45129c60cca37f420f5adbf65413d2f8a8d702/lib/streamlit/elements/form.py
[PW-input]: https://github.com/pywebio/PyWebIO/blob/b37a78d29625f7428116e01a36a2a400254cc8a2/pywebio/input.py
[PN-input]: https://github.com/holoviz/panel/blob/f2f87c06adfe1c477240b20454cc1f4469bb7414/panel/widgets/input.py
[PN-manifest]: https://github.com/holoviz/panel/blob/f2f87c06adfe1c477240b20454cc1f4469bb7414/pyproject.toml
