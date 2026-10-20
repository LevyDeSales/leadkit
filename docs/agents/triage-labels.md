# Vocabulário de triagem

As skills usam cinco papéis canônicos. Preserve os nomes abaixo e as labels públicas existentes;
a tabela descreve o vocabulário, não ordena criar labels ausentes.

| Papel da skill | Label no tracker | Quando usar |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Aguardando avaliação; também é o default dos templates de issues. |
| `needs-info` | `needs-info` | Faltam informações, contrato ou decisão para executar o escopo integral. |
| `ready-for-agent` | `ready-for-agent` | Escopo especificado; conferir dependências e autorização antes de executar. |
| `ready-for-human` | `ready-for-human` | Exige implementação ou atuação humana. |
| `wontfix` | `wontfix` | Decisão explícita de não realizar o trabalho. |

Na consulta pública de 19/10/2026, `needs-info`, `ready-for-agent` e `wontfix` estavam cadastradas;
`needs-triage` e `ready-for-human` permanecem defaults pertinentes, sem criação neste setup.
Confira o estado atual antes de aplicar uma label. Ausência não autoriza criar equivalente.

`adiada` significa aguardar retomada explícita pela mantenedora: não equivale a `wontfix` nem a
trabalho disponível. Preserve também as labels existentes `tipo/*`, `area/*` e `prioridade/*`,
sem substituir sua classificação por sinônimos ou novas taxonomias.

Antes de classificar um relato como bug, use `triage` para reunir fatos, reproduzir o comportamento
com dados sintéticos e distinguir defeito, dúvida e melhoria. Registre evidências e incertezas;
peça apenas a informação de produto que realmente falta. Atualizações no tracker seguem a
[autorização da tarefa](issue-tracker.md), não a disponibilidade de uma skill.
