"""Compilador da definição de formulário: um documento JSON vira o contrato (`FormSpec`) que o motor
consome. A API, o pipeline e os adaptadores não sabem de formulário nenhum em particular.

Uso
---

    spec = compile_definition(documento, version_number=3, clock=relogio)
    impressao = fingerprint(documento)

- `documento`: o objeto JSON já lido (um `Mapping`) ou o texto JSON (`str` ou `bytes`).
- `version_number`: o número da versão no registro (`FormSpec.version_number`). Dele saem o
  `version` e o `prompt_version` do contrato, os dois `<slug>-v<N>`.
- `clock`: devolve o agora, com fuso; o padrão é o relógio do sistema. O prompt lê a data de hoje
  em São Paulo dele a cada renderização, e a compilação o usa ao renderizar os exemplos.
- `fingerprint`: a impressão digital do conteúdo, o sha256 do documento em JSON canônico, que a
  versão publicada guarda.

A compilação levanta `DefinitionError`, com todos os problemas (`Problem`) do grupo em que parou,
nesta ordem: a estrutura do documento; as regras entre os campos; os exemplos; os templates. Cada
problema diz
o caminho do campo, com os índices das listas a partir de 0 (`steps.1.questions.0.options`), ou o
template e a linha (`prompt, linha 3`). O exemplo público está em `examples/demo.json`; cenários sintéticos de testes ficam em
`tests_app/fixtures/forms/`. Um cenário mínimo, sem a pontuação, fica em
`tests_app/fixtures/demo_definition.json`.

O formato (versão 1)
--------------------

Campos desconhecidos são recusados em todo nível. Nomes (`name`, chaves de `data`) usam letras
minúsculas sem acento, números e `_`, começando por letra; os nomes que o pydantic usa nos modelos
(`json`, `copy`, `model_dump`...) são reservados.

A versão publicada fica no banco num JSONB (`app.forms.stored`), e por isso:
- a ordem das chaves de um objeto não conta, porque o JSONB não a guarda. O que precisa de ordem vai
  numa lista, como as etapas, as perguntas e os campos do plano;
- o que o JSONB não guarda é recusado, com o caminho: NaN e Infinity, o caractere nulo (`\\u0000`)
  e a metade de um emoji (um surrogate sozinho). O aninhamento tem um teto, bem acima do de
  qualquer definição real.

- `format`: sempre 1. Muda quando o formato muda de um jeito que a versão anterior não lê.
- `slug`: o endereço público (`/p/<slug>`), com letras minúsculas, números e hífens (até 48).
- `title`, `intro` e `minutes`: o título (vai também ao PDF), o texto de entrada da página e o
  tempo estimado de resposta, de 1 a 60 minutos.
- `notices`, opcional: o aviso extra das páginas que a lead vê depois do envio, `waiting` (a de
  espera) e `failed` (a de falha, ao lado do código de atendimento), cada um opcional e com:
  - `title` e `text`: o título e o texto; uma linha em branco separa os parágrafos. É texto puro:
    a página o escapa, e não há HTML livre;
  - `link`, opcional: `label`, o texto do link, e `url`, o endereço, que só pode ser `https://` com
    domínio (sem espaços, barra invertida nem usuário e senha, até 2000 caracteres). O link abre em
    outra aba, sem referrer.
  O aviso é texto da página: não vai ao LLM, então mudá-lo não pede o teste do rascunho. A página
  mostra o aviso da versão que a lead respondeu, e nenhum formulário é citado nos templates. Sem
  `notices`, ou sem o aviso daquela página, ela sai como sempre.
- `steps`: as etapas, em ordem, cada uma com `title`, `help` opcional e `questions` (pelo menos
  uma). Cada pergunta tem:
  - `name` (a chave da resposta, única no formulário), `kind`, `label` e `help` opcional;
  - `required` (padrão `true`) e `contact` (padrão `false`: dado de contato fica fora do rascunho
    guardado no aparelho; a pergunta do nome da lead é de contato sempre, e e-mail e telefone
    também);
  - nos tipos de texto (`text`, `textarea`, `email`, `tel`): `placeholder` e `autocomplete`
    opcionais; `max_length`, obrigatório em `text` e `textarea` (padrão 254 no e-mail e 24 no
    telefone); e `min_length`, só em `text` e `textarea`;
  - nos de escolha (`choice`, uma opção; `choices`, várias): `options`, com `value` (o valor
    gravado, estável depois de publicado), `label`, `points` opcional (os pontos da opção, para a
    pontuação) e `show_when` opcional (a condição da opção); e, só em `choices`, `min_choices`
    (padrão 1) e `max_choices` (padrão: todas as opções);
  - `show_when`, opcional em qualquer pergunta: a condição para ela aparecer (ver "Perguntas
    condicionais").
- `identity`: as perguntas que identificam a lead. `email` (obrigatório) aponta para uma pergunta
  de e-mail obrigatória; `name`, para uma de texto; `phone`, para uma de telefone.
- `consent`: `version` (a versão do texto que o envio grava: mudou o texto, mude a versão),
  `processing` (a autorização obrigatória), `notice` (o aviso sobre o uso dos dados) e
  `marketing`, com o texto de cada canal oferecido (`email`, `whatsapp`). O WhatsApp só vale com
  `identity.phone`.
- `scoring`: a pontuação, opcional (ver "Pontuação").
- `instructions` e `prompt`: templates Jinja do texto que vai ao LLM (ver "Templates").
- `plan`: o modelo do plano, com `description` opcional e `fields`, a árvore de campos. A ordem
  da árvore é a ordem do schema, e por isso a ordem em que o LLM escreve. Cada campo tem `name`
  (único no grupo), `kind`, `label` opcional (o nome do campo como o admin o mostra na tela do
  pedido, com acento; sem ele, o admin mostra o `name`; só o admin o lê, o LLM, o plano gravado e o
  PDF não o veem, e os itens de uma lista não têm), `description` opcional e `internal` (padrão
  `false`), e conforme o tipo:
  - `text`: `min_length` e `max_length` opcionais, em caracteres; `min_paragraphs`, o mínimo de
    parágrafos (separados por linha em branco); e `forbidden_pattern`, `{pattern, message}`, uma
    expressão regular (módulo `regex`, sem diferença de maiúsculas, até 300 caracteres) que o texto
    do campo não pode conter, e o que a rodada de reparo diz quando ela aparece (padrão: «contém um
    trecho que este campo não aceita»). Cada texto tem 0,5 s para ser conferido; o que estoura é
    recusado, com a mensagem de que o padrão demorou demais;
  - `list`: `items` (os itens, um nó sem `name`, do tipo `text`, `group` ou `options`) e
    `min_items` e `max_items` opcionais;
  - `group`: `fields`, os campos do grupo, e `model` opcional, o nome do grupo no schema (padrão:
    o nome do campo em PascalCase; nos itens de uma lista, com `Item` no fim);
  - `options`: `values`, os valores fixos aceitos, ou `values_from`, de onde saem os valores,
    nunca os dois: o nome de uma lista dos dados do formulário (bloco `list`), ou uma lista de
    nomes, e então as opções são a união dos itens das listas, na ordem dos nomes e, em cada uma,
    na ordem dela; o item que está em duas listas aparece uma vez, onde surge primeiro. A lista de
    nomes não pode ser vazia nem repetir um nome, e cada nome precisa ser um bloco `list` dos
    dados. `in_window` liga valores a janelas das fases com datas (ver `phases`).
  O modo estrito da saída estruturada não leva limites ao LLM: o compilador escreve os limites e
  as quantidades no fim da descrição de cada campo ("Entre 3 e 120 caracteres.", "Exatamente 3
  itens.", "Pelo menos 3 parágrafos, separados por linha em branco."). Campo interno vai ao LLM,
  ao plano gravado e ao webhook, mas não ao PDF nem à prévia.

  `forbidden`, em `plan`, são as expressões que o plano não pode conter: uma lista de regras
  `{expressions, scope, fields}`. Cada expressão é um texto, procurado em qualquer parte do texto
  do campo, sem diferença de maiúsculas, acentos nem espaços. `scope` diz onde a regra vale: `all`
  (todos os textos do plano, o padrão), `only` (só nos `fields`) ou `except` (em todos menos nos
  `fields`). Cada campo de `fields` é o caminho dos nomes dele no plano (`titulo`, `fases.foco`;
  os itens de uma lista não entram no caminho). As regras, o padrão proibido e o mínimo de
  parágrafos valem onde quer que o plano seja validado: na resposta do LLM, no plano de exemplo
  e no PDF. A recusa diz o campo e a regra, nunca o texto do plano.
- `pdf`: `html` (o template do PDF), `css` (a folha de estilo, sem Jinja) e `scores`, os nomes
  das notas da pontuação que o PDF e a prévia podem mostrar (sem a chave, nenhuma; a prioridade e
  a persona nunca). As URLs relativas apontam para `app/assets/`, com o kit da marca em
  `brands/leadkit/`; fora dele, só `data:` e as imagens do formulário, que se pedem
  pelo nome (`asset:<nome>`, ver `app.forms.definition.images`). A definição guarda só o nome: o
  conteúdo de cada imagem a versão publicada fixa na publicação (`app.forms.assets`).
- `preview`: o template da prévia do plano na página de resultado, que também pode pedir imagens
  por `asset:<nome>`.
- `data`: os dados do formulário, blocos com nome e tipo declarado (`kind`), que as instruções e
  o prompt leem pelo nome do bloco (`data.edicao`). Os tipos (ver `app.forms.definition.data`):
  - `value`: `value`, um valor JSON livre, lido como está (`data.edicao.nome`);
  - `list`: `items`, uma lista de nomes sem repetição (iguais, a menos de maiúsculas, acentos e
    espaços), que um campo `options` do plano pode usar, sozinha ou com outras (`values_from`);
  - `windows`: `windows`, janelas `{name, label, start, end}` com nome único e datas AAAA-MM-DD,
    cada uma terminando no dia em que começa ou depois; `sequential` (padrão `true`) exige que
    cada janela comece depois do fim da anterior;
  - `edition`: `name` e, opcionais, `starts` e `ends`, que não vêm fora de ordem.
- `phases`: as fases com datas do plano, opcional (ver "Fases com datas").
- `examples`: `answers`, respostas de exemplo fictícias, validadas pelas perguntas da definição; e
  `plan`, o plano de exemplo, validado pelo modelo do plano. O plano de exemplo é o que o LLM falso
  devolve.

Perguntas condicionais
----------------------

Uma pergunta ou uma opção de escolha aparece só para certas respostas anteriores
(`app.forms.definition.conditions`). A condição é `show_when`:

    {"mode": "all", "rules": [{"field": "situacao", "operator": "in", "value": ["a", "b"]}]}

- `mode`: `all` (todas as regras valem) ou `any` (ao menos uma vale);
- `rules`: de 1 a 20 regras. `field` é o nome de uma pergunta anterior, e `operator` e `value`
  comparam a resposta dela: `equals` (igual a um texto), `in` (um dos itens de uma lista de textos;
  numa escolha múltipla, ao menos uma opção marcada está na lista) e `contains` (o texto da resposta
  tem o `value` dentro, com maiúsculas e minúsculas como estão; numa escolha múltipla, o `value` é
  uma das opções marcadas).

O que a lead vê sai de uma regra só, `Conditions.evaluate`, que percorre as perguntas na ordem e
tem de dar o mesmo resultado no navegador (`app/web/static/js/conditions.js`; os casos que os dois
resolvem estão em `tests_app/fixtures/conditions_cases.json`):
- o que está escondido, pergunta ou opção, conta como não respondido para as regras seguintes; por
  isso a pergunta A escondida esconde a C que depende dela;
- a pergunta escondida perde a resposta: o modelo das respostas a descarta, e ela fica **ausente**
  das respostas gravadas e do webhook (a pergunta que apareceu e ficou sem resposta segue como
  `null`). Uma pergunta obrigatória com condição só é exigida quando aparece;
- uma opção escondida não pode ser escolhida: o envio é recusado, com a mensagem no campo da
  pergunta;
- na pontuação, a pergunta escondida vale como a escolha opcional sem resposta: 0 ponto, 0
  escolhas, nenhuma opção marcada numa fórmula, e uma regra de persona por `answer` que a lê não
  vale e passa para a seguinte. No prompt, `answers.pergunta` e `labels.pergunta` dão `None`: o
  prompt trata a pergunta que pode ficar escondida.

Ao salvar, importar e publicar, o compilador recusa: um `field` que não é uma pergunta anterior; `in`
sem lista, ou outro operador com lista; `equals` numa escolha múltipla e `contains` numa escolha
única; um valor que não é uma opção da pergunta de escolha referida; e condição no e-mail, no nome e
no telefone da identidade. Mudar uma condição conta como mudança que vai ao LLM: a publicação pede
um teste.

Fases com datas
---------------

O plano segue o calendário real sem código (`app.forms.definition.phases`). O bloco `phases` tem:
- `days`: os dias de cada fase, em ordem, de 1 a 366 cada, até 12 fases. A quantidade de fases é o
  tamanho da lista. A primeira começa no dia da geração do plano, em São Paulo, e cada uma, no dia
  seguinte ao fim da anterior;
- `windows`: o nome de um bloco de janelas dos dados do formulário (`data`). O prompt recebe, fase
  a fase, a situação de cada janela dele;
- `plan_field`: o campo lista do plano com um item por fase (`fases`; dentro de um grupo,
  `roteiro.fases`). A lista precisa ter exatamente tantos itens quanto `days`.

Um campo de opções fixas do plano, dentro dessa lista, liga valores a janelas com `in_window`,
`{"valor": "nome_da_janela"}`. Um valor ligado só cabe nas fases em que a janela dele cruza a fase;
um valor sem ligação cabe em qualquer uma. O nome do campo é a chave dos valores permitidos no
prompt, então dois campos ligados a janelas não repetem o nome.

O prompt lê `phases`, uma lista com uma entrada por fase: `number`, `first` e `last` (as datas,
que os filtros `short_date` e `long_date` entendem), `period` ("8 de outubro a 6 de novembro de
2026"), `situation` (uma frase por janela: "Opção: está aberta e fecha nesta fase."),
`windows` (cada janela pelo nome, com `label`, `start`, `end`, `state`, `text` e `open`) e `allowed`
(os valores permitidos de cada campo ligado, pelo nome do campo). O `state` de uma janela é
`upcoming`, `opens`, `open`, `closes`, `opens_and_closes` ou `closed`. Sem `phases` na definição, o
prompt que lê `phases` não compila.

A conferência do plano recusa um valor fora da janela da fase dele, com uma mensagem por valor e
fase que cita só as posições (`fases.1.acoes.0.tipo`) e a regra, nunca o texto do plano, e a
rodada de reparo a recebe. Ela lê o relógio a cada plano, como o prompt. O plano de exemplo não
passa por ela: vale em qualquer data. Ao salvar, importar e publicar, o compilador recusa: bloco de
janelas ou lista de fases que não existe ou não é do tipo certo; lista com outra contagem de itens;
ligação que não está na lista de fases, ou que aponta para valor ou janela que não existe.

Pontuação
---------

Calculada a cada envio, com as respostas validadas; as notas, a prioridade e a persona vão para o
admin e para o webhook, nos mesmos campos de sempre (`scores`, `priority`, `persona`), e a
prioridade e a persona nunca chegam à lead. O bloco `scoring` tem:
- `scores`: as notas, em ordem (a ordem de `scores` no webhook). Cada uma tem `name`, `formula` e,
  opcional, `bands`: `key` e `label` são os nomes do nível e do rótulo que entram na pontuação ao
  lado da nota, e `levels`, do maior limite para o menor, cada um com `min` (vale se a nota é pelo
  menos isso), `key` e `label`; o último nível vale para o resto e não leva `min`;
- `priority` e `persona`: as regras, em ordem; vale a primeira cuja condição (`when`, uma fórmula
  verdadeira ou falsa) bate, e uma regra sem `when` (só a última) é o "senão". O resultado é
  `value` ou, na persona, `answer` (a opção marcada numa pergunta de escolha única). A prioridade
  é P1, P2 ou P3; a persona tem até 64 caracteres. Sem regras, o campo fica vazio.

As fórmulas (`app.forms.definition.formulas`) leem números, textos entre aspas, os pontos das
opções (`pontos(pergunta)`, a soma numa escolha múltipla) e a contagem de escolhas
(`contagem(pergunta)`), outras notas e os níveis das faixas, e as respostas de escolha única (a
opção marcada, um texto). Têm as quatro operações, `minimo`, `maximo`, `soma`, `media`,
`se(condição, a, b)`, `faixa(valor, limite1, resultado1, ..., padrão)`, as comparações e `e`, `ou`
e `nao`, e o arredondamento sempre explícito: `arredondar_meio_para_cima` (2,5 vira 3, como o
MCC) ou `arredondar_meio_para_o_par` (2,5 vira 2, como o reforma-2027). Nada fora dessa lista
existe: o analisador é próprio, sem `eval`.

Ao salvar, importar e publicar, o compilador recusa: nome repetido, igual ao de uma pergunta ou
reservado; construção fora da lista; referência a nota ou pergunta que não existe; `pontos` numa
pergunta com opção sem pontos; ciclo entre notas; tipos que não combinam; faixas fora de ordem;
regra de prioridade que não é P1, P2 ou P3; e as respostas de exemplo que dividem por zero ou que
não chegam a uma prioridade válida. Uma pergunta de escolha opcional sem resposta, ou escondida por
uma condição, vale 0 ponto e 0 escolhas. Se uma fórmula falha no envio de uma lead (uma divisão
por zero que os exemplos não pegaram), o pedido segue sem pontuação e o log diz o formulário e a
nota, nunca as respostas.

Templates
---------

Instruções, prompt, PDF e prévia rodam no ambiente isolado de `app.forms.sandbox`: variável
indefinida é erro, atributo interno (`__class__`, `_x`) é recusado e só há os filtros e as funções
da lista fechada, como `long_date`, `short_date` e `month_year` para as datas em português. O
contexto de cada um está em `app.forms.definition.templates`:
- instruções: `data`. Renderizam uma vez, na compilação;
- prompt: `answers`, `labels` (o rótulo da opção escolhida em cada pergunta de escolha), `scores`
  (as notas e os níveis das faixas das respostas, sem a prioridade e a persona), `today`, `data` e
  `phases` (as fases com datas, vazio sem `phases`);
- PDF e prévia: `plan` (sem os campos internos), `lead.name`, `form.title`, `form.slug`,
  `scores` (as notas do envio liberadas em `pdf.scores`, com o nível e o rótulo da faixa de cada
  uma), `generated_date` e `brand`, a biblioteca de componentes da marca
  (`app.forms.definition.components`: `brand.chip("Dias 1 a 30")`).

Na compilação, cada template é conferido: a sintaxe, os filtros, as variáveis, os campos lidos
direto de `answers`, `labels`, `scores`, `plan`, `lead`, `form` e `brand` (no PDF e na prévia,
`scores` só tem as notas liberadas) e, por fim, a renderização com os exemplos, com as notas das
respostas de exemplo.

Entre duas definições do mesmo formulário, `app.forms.definition.changes` diz o que mudou, seção
por seção, e o que mudou e vai ao LLM, que decide se a publicação de um rascunho pede um teste. A
pontuação (e os pontos das opções) só conta como "vai ao LLM" se o prompt lê `scores`. A seção a
seção e a decisão de "rascunho pendente" comparam a forma normal (`app.forms.definition.normal`):
o padrão do formato escrito por extenso (`"required": true`) e o omitido dizem a mesma coisa.
"""

from app.forms.definition.compiler import Clock, compile_definition, utc_now
from app.forms.definition.document import DefinitionError, Problem, fingerprint

__all__ = ["Clock", "DefinitionError", "Problem", "compile_definition", "fingerprint", "utc_now"]
