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

1. O helper comprova capacidade1 executando a CLI da imagem pelo seu ID `sha256`, sem rede/env.
2. Escala **API e worker para zero**, aguarda o fim real dos processos, inclusive tasks em saída,
   e confirma que não há container antigo executando. Atualiza os serviços ainda pausados para
   que também a **PreviousSpec tenha zero réplicas antes do DDL**. A pausa fica no Swarm se o
   comando for interrompido; não depende da memória de uma flag no script.
3. Aplica a migration com os serviços parados. O piso1 persistido nunca diminui. Não há API antiga
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
PreviousSpec e atestação**. Imagem sem contrato1 é recusada antes de alterar a stack ou o banco.
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

`capability` funciona sem configuração/rede e devolve `{"contract":1}`. Os outros comandos leem
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
| `image`, `contract` | ID `sha256` real e capacidade1 executada na imagem. |
| `previous_image`, `previous_contract` | ID/capacidade1 da imagem de retorno. |
| `replicas`, `tasks` | Quantidade positiva e lista completa das tasks atuais saudáveis. |
| `retired` | Tasks anteriores observadas com processo parado e estado terminal. |
| Cada task | `id`, `node_id`, `container_id`, `image`, `state`, `healthy`, `running`. |

Campos extras são recusados. Não guarde Spec/Env completos, URLs de destinos, dados de participante,
conteúdo de payload ou segredos. IDs de tasks/containers são evidência operacional, não dados da lead.
Ausência de observação fecha o gate. `/readyz`, conexão Hatchet ou uma réplica saudável não provam
compatibilidade de toda a frota. A atestação é uma declaração do host operacional autorizado; o
app valida o contrato e o epoch, sem ganhar acesso ao daemon.

O Swarm pode remover tasks e containers depois da escala para zero. Nesse caso, o helper só
comprova a saída se já observou o ID completo, o nó e a imagem imutável do container, a task
está terminal ou foi removida do inventário e o mesmo daemon local confirma a destruição.
Isso exige nó disponível, listagem local bem-sucedida sem aquele container e evento local
`destroy` do mesmo ID, com `timeNano` dentro da janela iniciada antes da observação. A consulta
de eventos tem fim absoluto; falha de `inspect`, ausência ou `DesiredState=remove` isolados
não comprovam parada. O ensaio CI reutiliza essa prova também antes do DDL e após a retomada.
Depois de `wait` ou de um `inspect` que falhe, o helper renova o inventário antes de decidir
se a task terminou ou foi removida. Os objetos recebidos precisam corresponder exatamente aos
IDs de tasks solicitados, sem omissões ou duplicados. Uma imagem diferente para um container
já observado é inconsistência e bloqueia o roteiro; não substitui a identidade conservada.

Eventos Docker guardam somente os últimos 256 registros globais; não são um histórico durável.
Evento ausente, antigo, perdido após restart, inválido ou de outro ID mantém o bloqueio.
Uma prova positiva já validada é conservada durante a execução, com nova conferência do nó
e inventário local em cada uso. O CI conserva somente essa evidência operacional na baseline
privada do ensaio para a conferência final, sem depender da retenção posterior do evento.
O roteiro não substitui essa prova por uma tag de imagem nem por uma lista vazia. Preserve
os processos antigos observáveis durante o ensaio; se a prova foi perdida, investigue no host
antes de retomar, sem apagar o piso ou forjar evidência.

O [backup.sh](backup.sh) usa o serviço DB da stack e grava dump no diretório configurado. Agende e
ensaie restauração em banco isolado. A baseline pública é para banco vazio; uma instalação existente
exige ensaio de compatibilidade próprio antes de qualquer promoção autorizada.
