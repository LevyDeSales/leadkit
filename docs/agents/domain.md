# Documentação do domínio

Leadkit tem um único contexto; não é monorepo. Antes de explorar uma área, leia
[GLOSSARY.md](../../GLOSSARY.md) e as [ADRs](../adr/README.md) que a afetam.
Use o vocabulário do glossário em specs, issues, propostas, código e testes.
Não introduza glossários por pacote nem `GLOSSARY-MAP.md`.

O glossário define conceitos; as ADRs registram contexto, decisão e consequências de escolhas
duráveis. Um termo ausente só exige atualização quando uma lacuna real for resolvida.
Se faltar um documento pertinente, prossiga com o contexto disponível e registre a incerteza;
não gere documentação de domínio apenas para preencher estrutura.

Conflito com ADR deve ser declarado com referência e motivo antes de propor substituição.
Considere o status e as sucessoras no índice: por exemplo, a ADR 0004 foi substituída pela 0013.
`domain-modeling` pode ajudar quando surgirem termos ou decisões reais; sua disponibilidade não
obriga uma revisão de arquitetura. Specs e tickets continuam no [tracker](issue-tracker.md).

Formulários e conteúdo são responsabilidade da operadora. Nossa responsabilidade é o código,
a segurança e os contratos que permitem operá-los. Preserve a equivalência funcional entre admin,
API e MCP nos serviços compartilhados, conforme a [ADR 0015](../adr/0015-api-de-admin.md).
Hatchet recebe e devolve só IDs e contagens, sem PII; migrations preservam o DDL aditivo.
Após a 0016, a exceção da [ADR 0017](../adr/0017-projecoes-e-finalidade-de-receptores.md)
exige adoção drenada e retorno somente com runtime compatível com o piso persistido, conforme
[qualidade e compatibilidade](product-quality.md) e o [roteiro Swarm](../../deploy/swarm/README.md).
Os detalhes técnicos estão em
[CONTRIBUTING.md](../../CONTRIBUTING.md).
