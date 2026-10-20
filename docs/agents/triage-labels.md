# Vocabulário de triagem

As skills usam cinco papéis canônicos. Preserve os nomes abaixo e as labels públicas existentes;
a tabela define o vocabulário. A coordenadora cria as labels canônicas ausentes como parte do
setup já autorizado, conferindo o estado atual antes de operar.

| Papel da skill | Label no tracker | Quando usar |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Aguardando avaliação; também é o default dos templates de issues. |
| `needs-info` | `needs-info` | Faltam informações, contrato ou decisão para executar o escopo integral. |
| `ready-for-agent` | `ready-for-agent` | Escopo especificado; conferir dependências e autorização antes de executar. |
| `ready-for-human` | `ready-for-human` | Depende de decisão, acesso ou atuação humana; não exige implementação humana por default. |
| `wontfix` | `wontfix` | Decisão explícita de não realizar o trabalho. |

`adiada` significa aguardar retomada explícita pela mantenedora: não equivale a `wontfix` nem a
trabalho disponível. Preserve também as labels existentes `tipo/*`, `area/*` e `prioridade/*`,
sem substituir sua classificação por sinônimos ou novas taxonomias.

Antes de classificar um relato como bug, use `triage` para reunir fatos, reproduzir o comportamento
com dados sintéticos e distinguir defeito, dúvida e melhoria. Registre evidências e incertezas;
peça apenas a informação de produto que realmente falta. Atualizações no tracker seguem a
[autorização da tarefa](issue-tracker.md), não a disponibilidade de uma skill.
