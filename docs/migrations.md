# Banco, atualização e preservação da instalação

O Leadkit usa PostgreSQL 16 e schema `public`. A distribuição começa na revisão `0012`, com uma
baseline estrutural explícita: banco novo ganha tabelas, índices, constraints e gatilhos, sem
formulário, destino, contato ou outro dado de negócio. O demo é importado explicitamente.

## Cadeia aditiva atual e rollback

O head único é `0018_journey_events`:
`0012` → `0013` → `0014_capture` → `0015_capture_delivery` → `0016_plan_audiences` →
`0017_continuation_release` → `0018_journey_events`. Não há head concorrente.
A revisão publicada `0013` acrescenta evidências de disponibilidade ao artefato PDF;
`0014_capture_receipts.py` acrescenta os recibos de captura, e `0015_capture_delivery` acrescenta
sua outbox, os perfis Baserow de captura e os eventos aceitos pelo webhook. As migrations e os
contratos publicados permanecem congelados; cada sucessora acrescenta seu contrato revisado.

A atualização preserva dados e versões publicadas; não transforma formulários existentes em
captura nem reescreve conteúdo. Historicamente, até `0015_capture_delivery`, o runtime anterior
com disponibilidade PDF continuava operando formulários PDF sobre o schema aditivo. Nessa
fronteira, integrações existentes conservavam a ativação e o perfil PDF; webhooks antigos
aceitavam apenas `plano.concluido`, sem backfill nem ativação implícita. A garantia de retorno ao
runtime anterior nessa etapa não se estende às revisões seguintes.

A `0016_plan_audiences` acrescenta audiência/finalidade dos receptores e o contrato operacional.
A `0017_continuation_release` acrescenta continuidade e liberação e confirma atomicamente o
piso de capacidade2 em fase `blocked`, limpando epoch e evidência anteriores. A
`0018_journey_events` acrescenta fontes, credenciais e eventos de jornada sem elevar esse piso.
Depois do commit da `0017`, runtime de capacidade0/1 é proibido, inclusive como retorno automático
ativo, mesmo que os formulários continuem automáticos e nenhuma participante tenha sido configurada.

Use uma imagem composta de capacidade2 que implemente continuidade e liberação para operar ou
recuperar essa fronteira. Um runtime2 compatível pode operar sobre schema `0018`; isso não torna
seu migrador antigo compatível com uma revisão que desconhece. O guard de um migrador sem contrato
para o novo head recusa legitimamente esse banco antes de escrita. No retorno, preserve o schema
e os envios confirmados, sem executar o migrador antigo nem fazer downgrade destrutivo. O
[roteiro Swarm de adoção composta de capacidade2](../deploy/swarm/README.md#adoção-composta-de-capacidade2)
é a referência autoritativa para atualização, atestação e recuperação no Swarm.

## Instalação em banco novo

Crie um banco e um usuário próprios, configure a aplicação e exporte `DATABASE_URL` no processo que
executará o Alembic. Na raiz do clone:

```sh
uv run alembic -c app/alembic.ini upgrade head
```

O Alembic não lê o `.env`. Um banco ocupado sem marcador compatível é recusado antes de escrita;
não apague tabelas nem use `stamp` para fazer o comando passar. Importe `examples/demo.json` pelo
admin, teste o rascunho e publique a versão desejada. Cada versão publicada fica imutável.

## Guard de compatibilidade

Toda execução online confere revisão e estrutura antes de executar migrations, inclusive quando
`upgrade head` seria no-op. O guard aceita banco vazio ou uma fronteira estrutural suportada pelos
contratos da distribuição. Revisão antiga/desconhecida, marcador inválido ou drift são recusados;
nenhum seed nem reparo de dados é aplicado automaticamente. SQL offline não é suportado.

A assinatura inclui constraints, índices, defaults, funções, gatilhos de imutabilidade, triggers
internos de integridade referencial e regras das tabelas. Não desabilite esses mecanismos para
contornar uma recusa. Mensagens de diagnóstico indicam objetos, sem imprimir SQL privado ou dados.
Cada migration aditiva futura precisa de contrato estrutural revisado na mesma release.

A baseline recusa downgrade. Voltar a versão do aplicativo não significa desfazer o banco:
compatibilidade do runtime de retorno deve ser ensaiada contra o schema aditivo e o piso
operacional antes da atualização. Depois do commit da `0017`, esse runtime precisa de capacidade2.

## Adoção por uma instalação existente

A adoção exige uma operação própria, planejada e autorizada por quem opera a instalação. Este
roteiro descreve a preparação; não executa deploy nem autoriza alterações em produção.

1. Preserve a fonte e a versão do código atualmente usadas, incluindo a referência de rollback.
   Guarde uma cópia verificável dos dados e dos objetos de storage em local privado; ensaie a
   restauração num ambiente isolado antes de atualizar.
2. Preserve formulários publicados, respostas, pedidos, artefatos e referências de imagens.
   Não importe novamente formulários existentes nem troque seus IDs ou versões para adotar o núcleo.
   A configuração da marca e seus assets continuam privados, externos ao repositório público.
3. Preserve configuração e chaves da instalação. Em especial, mantenha as chaves de cifra dos
   destinos/conexões e a política de rotação dos tokens; gerar valores novos não substitui uma
   migração de chaves. Não coloque segredos em comandos, logs, issues ou exemplos.
4. Confira a fronteira suportada usando uma restauração descartável e a cadeia original, quando
   necessária. Rode o guard da distribuição sobre essa cópia e compare a estrutura, os dados e os
   arquivos antes/depois. Não execute `stamp` para esconder divergência de schema.
5. Valide admin/API/MCP, respostas antigas, identidade por envio, geração de PDF, webhook e
   integrações com transportes fictícios. Ensaie o runtime de retorno sobre a cópia migrada,
   respeitando o piso2 após a `0017`; preserve uma imagem composta2 por ID/digest, além da fonte e
   configuração anteriores. A imagem0/1 preservada não serve como retorno ativo após esse commit.
6. Só depois da revisão e autorização da operadora, planeje a janela de adoção0/1→2 pelo
   [roteiro Swarm autoritativo](../deploy/swarm/README.md#adoção-composta-de-capacidade2).
   Antes de qualquer DDL, pause e drene API/worker, persista Current/Previous com zero réplicas e
   comprove positivamente a saída dos processos antigos; ausência no inventário não basta.
   A prova do helper se limita ao manager local observado; outros nós exigem tratamento próprio.
   Há indisponibilidade temporária nessa transição. A `0017` confirma piso2/blocked na transação;
   depois, instale novo epoch, normalize Current2/Previous2 e só ateste após as coletas coerentes.
   O novo núcleo não implica publicar formulários nem alterar destinos ou dados.
7. Em falha com commit desconhecido, leia revisão/status antes de escolher a imagem. Depois do
   commit da `0017`, mantenha os serviços parados e o gate bloqueado e recupere somente com imagem
   composta2; não baixe o piso nem reative runtime0/1. Depois da atestação, retorno também exige
   imagem composta2 compatível e revalidação de continuidade e liberação.

Se a revisão ou a estrutura não forem suportadas, interrompa o procedimento e planeje uma migração
aditiva revisada. Não limpe o banco da instalação para adaptar os dados ao exemplo público.

## Testes do banco

A suíte recria somente o schema de bancos descartáveis terminados em `_test`. Antes do DDL, o helper
confere tanto o nome na URL quanto `current_database()` da conexão. Esse reset vive no conftest e
não é um bypass nem uma opção do guard de produção. Tests usam fixtures fictícias independentes;
provas de convergência com cadeias privadas pertencem à instalação e não à distribuição pública.

## Manutenção pública dos contratos

Uma contribuição futura não depende de ferramentas ou cadeias privadas. Use uma cópia fixada da
última release pública suportada, a migration aditiva proposta e dois bancos de referência
próprios, descartáveis e terminados em `_test` (um para gerar, outro para verificar). Nunca congele
a assinatura a partir do banco da instalação que se pretende aceitar.

1. No primeiro banco vazio, aplique a release pública anterior pelo Alembic normal. A nova revisão
   ainda não entra nesse grafo: o guard recusa migrations sem contrato, inclusive em banco vazio.
2. Confira o contrato atual com `validate_database`. No mesmo banco de referência controlado,
   execute somente o `upgrade()` da migration proposta com `MigrationContext` e `Operations`.
   Essa execução é preparação de teste, fora do runtime: não altera nem desabilita o guard.
3. Leia `schema_signature` e guarde o resultado como dado Python revisável. O exemplo abaixo
   parte de `released/` (release pública fixada) e `draft_revision` (módulo público da migration
   proposta, já conferido pela contribuidora):
   Execute a receita num workspace de referência, com o app dessa release fixada em
   `PYTHONPATH` (por exemplo, `PYTHONPATH=released`), sem carregar outra instalação.

```python
import importlib.util
import os
import pprint
from pathlib import Path
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.migrations.compatibility import load_contracts, schema_signature, validate_database

module = importlib.util.spec_from_file_location("draft_revision", "draft/0015_example.py")
draft_revision = importlib.util.module_from_spec(module)
module.loader.exec_module(draft_revision)

url = os.environ["REFERENCE_DATABASE_URL"]
name = sa.engine.make_url(url).database
if not name or not name.endswith("_test"):
    raise RuntimeError("use somente banco de referência descartável terminado em _test")
engine = sa.create_engine(url)
try:
    with engine.begin() as connection:
        if connection.exec_driver_sql("SELECT current_database()").scalar_one() != name:
            raise RuntimeError("banco de referência divergente")
        validate_database(connection, load_contracts(Path("released/app/migrations")))
        with Operations.context(MigrationContext.configure(connection)):
            draft_revision.upgrade()
        signature = schema_signature(connection)
    Path("contract-reference.py").write_text(pprint.pformat(signature, sort_dicts=True) + "\n")
finally:
    engine.dispose()
```

A assinatura descreve estrutura, não dados nem o valor do marcador. Não use `stamp` para fabricar
uma fronteira: o banco gerador pode ser descartado após capturar a referência. Acrescente o valor
revisado a `SIGNATURES["nova_revisao"]`, preservando contratos anteriores, junto com a migration e
os modelos atualizados na mesma mudança.

4. No segundo banco vazio, teste a instalação do grafo público completo até o novo head. Teste
   também a atualização a partir da release anterior; um novo `upgrade head` deve ser somente de
   leitura, e drift deve continuar sendo recusado antes de escrita. Confira modelos com Alembic e
   execute a suíte completa. O teste público `test_future_contract_can_be_prepared_from_public_sources`
   demonstra esse ciclo com uma revisão temporária que não faz parte do produto.
5. Revise separadamente a migration, o delta do contrato, os testes e a compatibilidade do runtime
   anterior. Nenhuma opção de ambiente pode substituir contratos ou dispensar o preflight.

## Entregas de captura e rollback

A `0015_capture_delivery` não altera FKs das outboxes PDF. Acrescenta uma tabela vinculada à
submissão e a exatamente um destino concreto, com revisão, epoch e estado; não copia respostas,
URLs ou segredos. Retenção exclui as intenções junto da captura. Os contratos anteriores continuam
congelados e o novo contrato foi preparado em PostgreSQL 16 próprio a partir da fonte pública
`44ad0738c0eca62f9f2449df00fe99b975a109fa`, pela receita desta página.

Mantenha o schema no rollback de runtime. A garantia histórica de um runtime anterior operando o
ramo PDF com defaults aditivos vale até `0015`; captura e suas entregas já exigiam runtime que
entendesse essa finalidade. Depois do commit da `0017`, o piso2 proíbe runtime0/1 também para PDF.
Use imagem composta2 compatível com o schema `0018`, distinguindo seu runtime do migrador antigo
que recusa head desconhecido. Siga o [roteiro Swarm autoritativo](../deploy/swarm/README.md#adoção-composta-de-capacidade2)
para pausa, atestação e recuperação. Não faça downgrade destrutivo nem restaure backup perdendo
submissões confirmadas. Correção de configuração requer retry explícito da entrega, com revisão
esperada do mesmo destino. Remover uma conexão apaga suas intenções por cascade, sem apagar dados já enviados ao receptor.
