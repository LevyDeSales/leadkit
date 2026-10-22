# Deploy com Docker Swarm

Este exemplo pressupõe Swarm inicializado, Python 3.11 ou posterior no host e Traefik gerenciado
pela operadora. Confira rede, entrypoint e certresolver em [stack.yaml](stack.yaml). API e worker
compartilham banco, storage e, quando houver, o overlay de marca somente leitura. Use domínio,
banco e segredos próprios e ensaie a atualização em ambiente isolado antes da instalação.

Copie **`stack.yaml`, `deploy.sh` e `rollout.py` juntos** para uma pasta de configuração fora do
código. Copie os modelos `app.env.example` e `db.env.example` como `app.env` e `db.env`, preencha-os
e restrinja ambos a modo `600`. `DATABASE_URL` deve apontar para o serviço Postgres da mesma stack
e usar o usuário/banco de `db.env`. Escolha `STACK_NAME` e domínio próprios. Não reutilize a pasta
de outra stack.

Construa a imagem, por exemplo `docker build -t leadkit:VERSAO .`, ou carregue uma imagem validada.
Execute da pasta de configuração:

```sh
IMAGE=leadkit:VERSAO ./deploy.sh
```

[deploy.sh](deploy.sh) conserva as validações de nome, domínio, rede confiável do Traefik e limites
de recursos e chama [rollout.py](rollout.py). O helper roda **no host operacional**, com o Docker
CLI já autorizado. Não monte Docker socket no app nem dê à API/worker acesso operacional.
Este roteiro só atesta tasks e containers do **manager local**: task em outro nó, nó indisponível,
container sem identificação ou inventário incompleto interrompem a operação. Multinó exige outro
canal operacional comprovado; este script não cria SSH ou acesso remoto como alternativa.

## Primeira adoção do contrato de projeções

A migration `0016_plan_audiences` introduz o piso1 e deixa participante bloqueada. O código antigo
não conhece audiência e pode enviar o plano integral. Portanto, na primeira adoção:

1. O helper comprova a capacidade conhecida da imagem pelo ID `sha256`, sem rede/env. A distribuição
   composta atual anuncia2; uma instalação nova chega ao piso2 ao aplicar toda a cadeia.
2. Escala **API e worker para zero**, aguarda o fim real dos processos, inclusive tasks em saída,
   e confirma que não há container antigo executando. Atualiza os serviços ainda pausados para
   que também a **PreviousSpec tenha zero réplicas antes do DDL**. A pausa fica no Swarm se o
   comando for interrompido; não depende da memória de uma flag no script.
3. Aplica a cadeia de migrations com os serviços parados. O piso persistido nunca diminui. Não há API antiga
   capaz de criar/alterar/rotacionar destino nessa janela nem worker antigo com payload em memória.
4. Sobe a imagem compatível ainda com o gate bloqueado e faz uma segunda atualização dos serviços.
   Só aceita habilitar participante quando **Spec e PreviousSpec de API e worker são compatíveis**,
   todas as réplicas esperadas estão saudáveis e a saída dos processos anteriores foi observada.
5. Recolhe o inventário novamente, recusa desvio de Spec/tasks/saúde e atesta o epoch corrente no
   banco. A API pode então aceitar a configuração participante.

Há indisponibilidade temporária na primeira adoção. Não prometa zero downtime nesse corte.
Em banco novo, a stack começa com API/worker pausados, o helper aguarda o banco, migra e só depois
sobe o app. Não use `docker stack deploy` à mão para contornar essa ordem.

Nas atualizações seguintes entre imagens compatíveis, continua o rolling `start-first`. Antes de
alterar a stack, o helper bloqueia novas entregas participantes e espera as transações de envio já
em voo pela mesma trava de configuração. As entregas internas autorizadas seguem o contrato
existente. Falha conserva intenções e configurações; repetir entrega não gera outro plano/PDF.

## Imagens, retorno e recuperação

A capacidade vem de **executar o comando da imagem exata**, nunca de um label arbitrário.
O deploy usa um RepoDigest quando disponível. Builds locais sem registry recebem a referência
`leadkit-local:<hash-do-ID>` no daemon local; cada container é conferido contra o ID efetivamente
validado. Essa referência precisa permanecer disponível para restart e rollback. Tags de entrada
reaproveitadas resolvem uma nova referência de conteúdo e passam pelas mesmas verificações; o
helper não aceita silenciosamente uma réplica que continue com outro ID. A documentação Docker
explica por que [digests fixam a imagem e tags podem variar](https://docs.docker.com/engine/swarm/services/).

Guarde imagens compatíveis e backup verificado. O retorno suportado usa a mesma pasta/scripts:

```sh
ROLLBACK=1 IMAGE=leadkit:VERSAO_COMPATIVEL ./deploy.sh
```

`ROLLBACK=1` pula migrations, mas **não pula o piso, as provas de capacidade, réplicas, saúde,
PreviousSpec e atestação**. Imagem abaixo do piso persistido é recusada antes de alterar a stack ou o banco.
Depois da0016 não existe recuperação suportada que restaure imagem0 com réplicas ativas, mesmo
sem participante criada ou com o gate bloqueado.

- Falha depois da pausa e antes da migration: mantenha zero e retome o roteiro com imagem válida.
- Falha depois da migration: mantenha zero ou somente imagens compatíveis e corrija para frente.
  Não restaure réplicas antigas; o retorno automático inicial só pode apontar à Spec antiga pausada.
- Falha durante atualização compatível: o Swarm pode retornar à PreviousSpec compatível. O helper
  acusa a falha e deixa o gate bloqueado; corrigida a causa, execute o roteiro novamente.
- Interrupção/restart do deploy: o banco conserva piso/fase/epoch e o Swarm conserva a pausa ou Spec.
  O novo comando serializado usa outro epoch; resposta antiga não reabre a habilitação.
- Restart normal de uma task na mesma Spec/imagem compatível: não reduz piso nem exige apagar dados.
  O processo confere o piso antes de preparar destinos ou registrar workflows.

O arquivo `.deployment.lock` serializa comandos da pasta pelo lock do sistema, liberado quando o
processo termina. Preserve uma pasta autoritativa por stack e não execute mutações Docker paralelas.
Comandos Docker/SQL deliberados que ignorem esse roteiro estão fora da garantia; um binário antigo
que ignora estado novo não pode ser protegido por uma flag nova no app. Não apague destinatárias,
faça downgrade, restaure banco ou use `stamp` para viabilizar retorno.

## CLI da imagem e contrato da evidência

A CLI não lê `.env`, não chama provedores/Hatchet e não consulta Docker:

```sh
python -m app.deployment_contract capability
python -m app.deployment_contract status
python -m app.deployment_contract begin
python -m app.deployment_contract attest
```

`capability` funciona sem configuração/rede e devolve `{"contract":2}` na imagem composta atual. Os outros comandos leem
somente `DATABASE_URL` do ambiente; no roteiro, o Docker a recebe pelo `--env-file app.env`, sem
valor em argv/log. `status` é somente leitura e devolve `minimum_contract`, `phase` e `epoch`.
Tabela ausente é bootstrap0; tabela presente sem singleton é piso conservador1 e bloqueio.

`begin` lê **stdin JSON** com exatamente `{"epoch":"UUID"}` e fecha o gate sem reduzir o piso.
`attest` lê exatamente `{"epoch":"UUID","evidence":{...}}`. Sucesso de escrita é `{"ok":true}`.
A saída de erro é sempre JSON `{"error":{"code":"deployment_contract_blocked","message":"..."}}`,
com mensagem fixa e sem detalhes recebidos; exit1 indica recusa/indisponibilidade e exit2 entrada
inválida. Não há traceback de SQL/configuração. stdin tem limite de64KiB.

A evidência é gerada pelo helper, sem preenchimento manual no app:

| Campo | Conteúdo permitido |
| --- | --- |
| `schema_version`, `stack`, `node_id` | Versão1, nome da stack e ID do manager local. |
| `services.api`, `services.worker` | Ambos obrigatórios, com o mesmo contrato abaixo. |
| `id`, `spec_version`, `observed_spec_version` | Serviço e versão observada novamente; versões precisam coincidir. |
| `image`, `contract` | ID `sha256` real e capacidade conhecida executada na imagem. |
| `previous_image`, `previous_contract` | ID/capacidade conhecida da imagem de retorno. |
| `replicas`, `tasks` | Quantidade positiva e lista completa das tasks atuais saudáveis. |
| `retired` | Tasks anteriores observadas com processo parado e estado terminal. |
| Cada task | `id`, `node_id`, `container_id`, `image`, `state`, `healthy`, `running`. |

Campos extras são recusados. Não guarde Spec/Env completos, URLs de destinos, dados de participante,
conteúdo de payload ou segredos. IDs de tasks/containers são evidência operacional, não dados da lead.
Ausência de observação fecha o gate. `/readyz`, conexão Hatchet ou uma réplica saudável não provam
compatibilidade de toda a frota. A atestação é uma declaração do host operacional autorizado; o
app valida o contrato e o epoch, sem ganhar acesso ao daemon.

O Swarm pode remover tasks e containers depois da escala para zero. O helper conserva a
inspeção positiva de encerramento: `State.Running=false`, `StartedAt` e `FinishedAt` válidos,
com início ≤ fim ≤ observação, `ExitCode` inteiro válido e identidade real do container. A ordem
preserva os nanossegundos do Docker; um encerramento histórico anterior à observação é permitido.
A prova vincula task, CID completo, imagem imutável, nó, início da execução e instante da
observação. Inspeções idênticas do mesmo encerramento conservam a mesma prova, inclusive o
`observed_at`, para permitir comparação estável de snapshots semeados com `exit_proofs`.
Um novo `StartedAt`, término ou código de saída substitui a prova; processo vivo, identidade
diferente ou inspeção contraditória invalidam a anterior. Se o container desaparecer depois, essa prova
permite confirmar a saída sem depender de um evento `destroy` ainda retido. Cada uso exige
novamente nó disponível, daemon local identificado, listagem local bem-sucedida sem aquele
container e Task terminal ou removida do inventário.

Sem inspeção positiva de encerramento conservada, a alternativa continua exigindo imagem e
CID previamente observados e evento local `destroy` do mesmo ID, com `timeNano` dentro da
janela iniciada antes da observação, além das mesmas verificações de nó e inventário. A consulta
de eventos tem fim absoluto; falha de `inspect`, ausência ou `DesiredState=remove` isolados
não comprovam parada. O ensaio CI reutiliza essa prova também antes do DDL e após a retomada.
Depois de `wait` ou de um `inspect` que falhe, o helper renova o inventário antes de decidir
se a task terminou ou foi removida. Se uma saída positiva já foi comprovada mas a Task ainda
não ficou terminal, a coleta com espera concede até 30 segundos por task para convergência,
inclusive quando o container desapareceu durante `wait` e há evento `destroy` local válido,
contados após o retorno de `docker wait` quando necessário, conferindo novamente o inventário;
prazo esgotado mantém o bloqueio. `docker wait` e `DesiredState`
isolados nunca substituem a prova do processo. Os objetos recebidos precisam corresponder
exatamente aos IDs de tasks solicitados, sem omissões ou duplicados. Uma imagem diferente para um container
já observado é inconsistência e bloqueia o roteiro; não substitui a identidade conservada.
Se esse refresh descobrir outra task durante a coleta, o helper recusa a coleta incompleta
antes de comprovar a pausa ou executar DDL. Repita o roteiro após investigar o inventário;
não há tentativa automática que possa omitir o processo recém-descoberto.

Eventos Docker guardam somente os últimos 256 registros globais; não são um histórico durável.
Sem inspeção positiva conservada, evento ausente, antigo, perdido após restart, inválido ou
de outro ID mantém o bloqueio.
Uma prova positiva já validada é conservada durante a execução, com nova conferência do nó
e inventário local em cada uso. O CI conserva somente essa evidência operacional na baseline
privada do ensaio para a conferência final, sem depender da retenção posterior do evento.
O roteiro não substitui essa prova por uma tag de imagem nem por uma lista vazia. Preserve
os processos antigos observáveis durante o ensaio; se a prova foi perdida, investigue no host
antes de retomar, sem apagar o piso ou forjar evidência.

O [backup.sh](backup.sh) usa o serviço DB da stack e grava dump no diretório configurado. Agende e
ensaie restauração em banco isolado. A baseline pública é para banco vazio; uma instalação existente
exige ensaio de compatibilidade próprio antes de qualquer promoção autorizada.

### Adoção composta de capacidade2

A imagem2 contém continuidade e liberação juntas. Não use imagem parcial apenas porque sua CLI
anuncia2. A `0017_continuation_release`, sucessora da0016, grava schema aditivo e piso2 bloqueado
atomicamente, apagando epoch/evidência anteriores. Nunca altere assinaturas antigas nem use stamp.

O helper compara a capacidade executada pelo ID imutável com o piso persistido antes de tag,
migration, begin ou alteração da stack. Alvo abaixo do piso e rollback que elevaria o piso são
recusados. Piso0 ou alvo acima do piso exigem pausa/drenagem antes do DDL. Recuperação com
Current/Previous abaixo do piso também repete a prova, mesmo quando suas réplicas já estão em zero.
Quando bootstrap ou elevação de piso já exigem pausa, o helper não consulta antecipadamente a
capacidade das referências antigas para tomar essa mesma decisão. PreviousSpec ausente permanece
sem identidade/capacidade atribuída; pausa, prova de saída e normalização do retorno em zero
continuam obrigatórias antes do DDL, assim como Current/Previous compatíveis antes da atestação.
Ausência de processo sem prova positiva, inventário desconhecido ou réplica antiga viva mantém
os serviços bloqueados. A prova continua limitada ao manager local observado.

Em1→2: begin fecha o gate sem elevar piso; quiesce persiste Current/Previous em zero; a única
migration composta confirma piso2/blocked; begin instala novo epoch porque o DDL limpou o anterior;
instalação/force normaliza Current2/Previous2; duas coletas coerentes precedem attest. Os campos
contract/previous_contract do manifesto aceitam1/2 conhecidos e precisam ser pelo menos o piso
persistido. Attest só confirma evidência/instante/fase, sem promover ou reduzir o piso.

Falha antes do commit SQL permite retomar o upgrade ou reinstalar/atestar1 enquanto o piso seguir1.
Commit desconhecido exige ler revisão/status antes de escolher imagem. Depois do commit0017,
permaneça em0/blocked e recupere somente com imagem composta2; imagem1 está proibida inclusive
como retorno automático ativo. Falha antes da atestação mantém2/blocked, com novo epoch na retomada.
Depois da atestação, retorno usa imagem composta compatível2 e revalida ambas as funcionalidades.
Atualizações2→2 com Current/Previous compatíveis preservam rolling. Nenhum /readyz substitui
schema, piso, prova de saída ou atestação. Esta documentação não afirma ensaio de uma instalação.
