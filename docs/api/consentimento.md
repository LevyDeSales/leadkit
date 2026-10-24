# Consentimento por versão do formulário

A configuração existente `consent` admite `acquisition_method: checkbox | submit`,
`presentation: panel | compact` e `position: first_step | before_submit`. Omitir esses
campos conserva caixa, painel e primeira etapa; a serialização de definições legadas não
acrescenta essas opções. Método, textos e canais alteram a revisão semântica do aviso;
apresentação e posição alteram apenas a versão do formulário. Salvar não publica.

O POST público existente recebe o payload legado `{privacy:true,text_version:...}` somente
em versões checkbox. O novo `consent` discrimina a declaração:

```json
{"method":"checkbox","action":"confirm","privacy":true,"text_version":"pesquisa-v2","marketing_email":false,"marketing_whatsapp":false}
```

```json
{"method":"submit","action":"submit","text_version":"pesquisa-v2","marketing_email":false,"marketing_whatsapp":false}
```

O método esperado vem da versão resolvida no servidor. O ramo submit recusa `privacy`,
`checked` e extras; caixa exige `privacy:true`. Método incompatível e canal não oferecido
em uma submissão nova retornam 422, sem gravação. Aviso obsoleto em um envio novo retorna
409 `consent_text_outdated`: reler o aviso
e enviar novamente conforme o método; não solicitar uma caixa inexistente. A página HTML
usa a mesma validação e deriva a declaração do POST final válido, com e sem JavaScript.
GET, prévia, recarga, navegação intermediária e validação recusada não registram aceite.

Novos envios, inclusive os recebidos pelo payload legado de caixa, persistem `evidence_schema:2`, `method`, `action`, `source`,
`text_version`, flags reais de marketing e `privacy_at` UTC na transação das respostas.
Ambos os métodos aceitos projetam `consent.privacy=true`. O adaptador atribui
`public_html` ou `public_api_declaration`; nenhuma origem enviada no corpo é admitida.
A origem HTTP descreve uma declaração recebida, sem certificar leitura, pessoa ou clique.
Não se coletam IP, user-agent ou gravação de sessão para essa evidência.

Replay discriminado compara respostas canônicas, método, ação, texto e marketing;
divergência retorna 409 `idempotency_conflict`, antes das verificações de método, texto e
canal aplicadas a envios novos. Um payload estruturalmente inválido continua retornando 422
antes dessa comparação. Horário e origem são os do primeiro aceite,
inclusive em retry por outro transporte. Publicação concorrente e replay usam a versão
histórica fixada na submissão. Shapes e replay de registros históricos legados continuam sem método/origem
inventados; retry não atualiza suas escolhas de marketing.

Detalhes de pedidos/capturas e JSON autorizado de webhook/planilha conservam a evidência.
A API de pedidos acrescenta método, ação, origem e esquema apenas quando registrados;
`privacy_accepted_at` descreve o aceite no servidor para ambos os métodos. Os detalhes
continuam no bloco protegido `lead_data`, com papel total e opt-in; MCP conserva suas
flags e confirmações. O mapeamento valida evidência discriminada e não aceita objeto
truthy em `processing` como autorização. Nenhuma operação administrativa nova foi criada.

## Adoção e retorno

O parser anterior recusa campos desconhecidos de definição, inclusive essas três opções.
Aceitar definições antigas no runtime novo não torna o runtime antigo capaz de ler uma
versão nova. Atualize e confira todas as instâncias leitoras/escritoras antes de publicar
configurações novas; retorno após essa publicação exige imagem que compreenda o contrato.
Conserve versões imutáveis e a evidência histórica. JSONB já acomoda a evidência aditiva:
esta entrega não exige migration SQL nem modifica/reduz o piso operacional persistido.
A revisão de compatibilidade precede DEV; publicação de formulário exige autorização própria.
