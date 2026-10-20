# Cobertura da API de admin

Matriz pública de rotas, operações, papéis e efeitos. Os testes conferem as declarações contra
o OpenAPI e exigem uma operação ou justificativa para cada rota.

## Como ler

- **Rota atual:** o método e o caminho que o app registra hoje (`{param}` como no código). Cada
  registro de rota é uma linha, e uma função com duas rotas (o destino padrão e o de um formulário)
  tem duas linhas.
- **Serviço hoje:** a função que já tem a regra de negócio, em `módulo.função`. "Presa na rota" é a
  lógica que hoje só existe dentro da rota HTML (conferência, mensagem, disparo): a API não a
  reimplementa, a fase do recurso a extrai antes (pela camada de serviços). "Consulta com rótulo" é
  uma leitura de `app/admin/views.py` que já devolve texto de tela (rótulo em português, data de
  Brasília): a API precisa da mesma consulta sem o rótulo.
- **Operação da API:** método e caminho **relativos a `/admin/api/v1`**; `operation_id` em inglês,
  `snake_case`, verbo e recurso.
- **Papel:** `leitura` ou `total`, o mínimo que a chave precisa. Montar o prompt, pontuar e
  pré-visualizar não gravam nada e contam como `leitura` (a prévia que compila um template mandado no corpo
  é `total`). **Dado pessoal da lead só sai para
  `total`**: `leitura` vê configuração, contagens e estados (ver "Pedidos", abaixo).
- **Marcas** (`x-*` no OpenAPI): `destrutiva` (`x-destructive`: apaga ou sobrescreve algo difícil de
  desfazer), `externo` (`x-external-effect`: muda o que a lead vê, para onde os dados dela vão ou
  chama um serviço de fora, às vezes pago) e `lead` (`x-lead-data`: a operação pode trazer dado
  pessoal da lead, só para `total`). **Nas marcadas `destrutiva` ou `externo`, a API exige
  `"confirm": true` (`422 confirmation_required` sem ele), mesmo onde a página não pede; o MCP também
  pede confirmação à pessoa.** A simulação de publicação (`dry_run`) não exige: a dispensa é do roteador, na
  mesma declaração da marca (`confirm_unless`), e vai ao OpenAPI como `x-confirm-unless`.
- **Status:** `falta` (sem operação ainda), `feita · <arquivo>::<teste>` (a operação existe, está no
  `openapi.json` e o teste que a prova está citado) ou `fora (letra)`, uma rota sem par, com o motivo
  em ["O que não entra na API"](#o-que-não-entra-na-api-e-por-quê).

## Totais

| Recurso | Rotas | Funções | Operações da API | Fora |
| --- | ---: | ---: | ---: | ---: |
| Acesso (login e sessão) | 3 | 3 | 0 | 3 |
| Pedidos | 6 | 6 | 6 | 0 |
| Capturas | 2 | 2 | 2 | 0 |
| Destinos do webhook | 11 | 6 | 6 | 0 |
| Formulários: cadastro, situação e retenção | 10 | 10 | 8 | 2 |
| Versões | 3 | 3 | 3 | 0 |
| Rascunho: editor | 6 | 6 | 5 | 1 |
| Rascunho: PDF e prévias | 4 | 4 | 2 | 0 |
| Rascunho: testes | 8 | 8 | 7 | 0 |
| Rascunho: publicação | 2 | 2 | 2 | 0 |
| Integração com o Baserow | 8 | 8 | 8 | 0 |
| Imagens dos formulários | 5 | 5 | 5 | 0 |
| Chaves de API | 3 | 3 | 3 | 0 |
| Configurar o MCP | 2 | 2 | 0 | 1 |
| **Total** | **73** | **68** | **57** | **7** |

- **73 rotas** (`GET` e `POST`; o admin não usa outro método), de **68 funções**: as cinco ações dos
  destinos (ver, endereço, segredo, remoção e teste) existem para o destino padrão e para o de um
  formulário, com a mesma função.
- **66 rotas têm par na API**, em **57 operações** (algumas rotas dividem a operação: as duas dos
  destinos, o editor do PDF e o do formulário, que salvam o mesmo rascunho, a página do teste e o
  trecho do polling, e a criação de chave da página das chaves e a da tela do MCP). **7 rotas ficam
  fora**: o login e a saída (a chave substitui a sessão), três telas sem dado nem ação e a tela de
  configurar o MCP.
- O editor estruturado do rascunho tem **48 operações** dentro de uma rota só (`POST
  /admin/forms/{slug}/draft/edit`): 42 viram uma edição do documento e 6 são só redesenho da tela
  ([a tabela](#operações-do-editor-48)).
- Mais **2 operações novas**, sem rota no admin (`get_current_api_key` e `list_audit_log`): ver
  [as novas](#operações-novas-sem-rota-no-admin).
- Total da API: **59 operações** (57 + 2), as mesmas do `docs/api/openapi.json` (o teste de cobertura
  confere). **Nenhuma linha está `falta`**: a cobertura está fechada.

## Inventário por recurso

### Acesso

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/login` | Mostra o formulário de login; com sessão válida, vai para `/admin` | `AdminAuth.read` | — | — | — | — | fora (A) |
| `POST /admin/login` | Confere usuário e senha (argon2), bloqueia o IP após 5 falhas, abre a sessão com o token CSRF | `AdminAuth.check_credentials`, `LoginThrottle` | — | — | — | — | fora (A) |
| `POST /admin/logout` | Encerra a sessão e apaga o cookie | `AdminAuth` | — | — | — | — | fora (A) |

### Pedidos

**Dado da lead.** `list_jobs` e `get_job` são `leitura` e devolvem o pedido sem dado pessoal: código,
situação, etapa, tentativa, formulário e versão, datas, código da falha, estado de cada entrega e de
cada sincronização, e a linha do tempo sem o texto livre dos motivos. Nome, e-mail, telefone,
respostas, pontuação, consentimento e plano ficam num bloco `lead_data`, que só vem com
`include_lead_data=true` e só para `total` (`403 forbidden_role` para `leitura`). A busca por nome ou
e-mail pede `total`; `leitura` busca só pelo código. `download_job_pdf` entrega o plano da lead e é
`total`.

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin` | Lista os pedidos (busca por nome, e-mail ou código; filtros de status, de formulário (`form=<endereço>`) e da versão dele (select "Versão", só com um formulário escolhido; `form_version=<texto>`, o texto que a coluna mostra; o mesmo parâmetro na API, que o recusa sem `form`), de quando o pedido chegou (select "Recebido", `period` com `today`, `7d` ou `30d`, em dias de calendário de Brasília; na API, `created_from` e `created_to`) e da entrega ao webhook da tentativa atual (select "Entrega", `delivery=failed` ou `pending`; o mesmo parâmetro na API) e da sincronização com a planilha da tentativa atual (select "Planilha", `sync=failed` ou `pending`; o mesmo parâmetro na API); 25 por página; a coluna Formulário mostra o título e a versão; a coluna "Entregas e planilha" mostra a entrega ao webhook e a sincronização com a planilha da tentativa atual, e na API cada item traz `deliveries` e `syncs`, só com destino ou tipo e estado) e avisa dos formulários sem destino e dos segredos que não abrem (o aviso vai para `list_destinations`). Na API, sem dado pessoal para `leitura` | `queries.jobs` (`JobFilters`, os filtros, e a consulta que `views.list_jobs` decora) e `views.destinations_overview` (consulta com rótulo) | `GET /jobs` | `list_jobs` | leitura | lead | feita · tests_app/test_admin_api_jobs.py::test_list_jobs_is_an_envelope_of_the_newest_first_with_the_state_and_no_lead_data |
| `GET /admin/jobs/{public_id}` | O pedido: lead, respostas, pontuação, plano, PDFs, entregas, sincronizações e linha do tempo das tentativas. Na API, a lead, as respostas, a pontuação e o plano ficam no bloco `lead_data`, só para `total` | `queries.jobs` (a consulta que `views.job_detail` decora) | `GET /jobs/{public_id}` | `get_job` | leitura | lead | feita · tests_app/test_admin_api_jobs.py::test_get_job_a_read_key_reads_the_state_of_every_order_and_no_lead_data |
| `POST /admin/jobs/{public_id}/reprocess` | Nova tentativa auditada, com motivo, disparada uma vez; recusa se a tentativa mudou | `actions.jobs.reprocess`: o motivo de 10 a 300 caracteres, o `confirm`, a tentativa esperada, `jobs.reprocess` e o disparo (`launching.launch_attempt`), com a diferença entre enfileirada e adiada para o reconciliador | `POST /jobs/{public_id}/reprocess` | `reprocess_job` | total | externo | feita · tests_app/test_admin_api_job_actions.py::test_reprocess_job_creates_the_next_attempt_and_answers_with_the_new_state |
| `POST /admin/jobs/{public_id}/deliveries/{delivery_id}/resend` | Entrega que falhou volta para pendente e é disparada de novo | `actions.jobs.resend_delivery`: `jobs.resend_delivery` e o disparo (`launching.launch_delivery`, numa thread, com o limite de `HATCHET_TRIGGER_TIMEOUT_SECONDS`) | `POST /jobs/{public_id}/deliveries/{delivery_id}/resend` | `resend_job_delivery` | total | externo | feita · tests_app/test_admin_api_job_actions.py::test_resend_job_delivery_returns_the_failed_delivery_to_pending_and_launches_it |
| `GET /admin/jobs/{public_id}/pdf` | PDF de uma tentativa (a última, sem `version`), como anexo; é o plano da lead, então só `total` | `actions.jobs.pdf_file`: a tentativa, o 404 sem PDF, o 410 se a retenção o apagou, e a URL assinada de 300 s (a página) ou os bytes (a API) | `GET /jobs/{public_id}/pdf` | `download_job_pdf` | total | lead | feita · tests_app/test_admin_api_jobs.py::test_download_job_pdf_gives_the_bytes_of_the_latest_attempt_as_an_attachment |
| `POST /admin/jobs/{public_id}/syncs/{sync_id}/resend` | Sincronização com a planilha que falhou volta para pendente e é disparada de novo | `actions.jobs.resend_sync`: `integration_syncs.resend` e o disparo (`launching.launch_sync`) | `POST /jobs/{public_id}/syncs/{sync_id}/resend` | `resend_job_sync` | total | externo | feita · tests_app/test_admin_api_job_actions.py::test_resend_job_sync_returns_the_failed_sync_to_pending_and_launches_it |

### Destinos do webhook

O escopo é o destino padrão ou o de um formulário. Na API o escopo é o parâmetro `{scope}`: a palavra
`default` ou o slug do formulário ([ADR 0015](../adr/0015-api-de-admin.md#destinos-um-escopo-uma-família-de-operações)).
`default` é slug reservado desde a fase 3.1. A chave `leitura` recebe a URL do destino só com o esquema e o host
(`https://host`), porque o caminho costuma valer como senha; a `total` a recebe inteira.

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/destinations` | Visão de todos os destinos: o padrão, o de cada formulário, quem não tem destino e quem tem segredo que não abre | `queries.destinations.overview` (a consulta que `views.destinations_overview` decora) | `GET /destinations` | `list_destinations` | leitura | — | feita · tests_app/test_admin_api_destinations.py::test_list_destinations_with_none_gives_the_default_first_and_every_form_unserved |
| `GET /admin/destinations/default` | O destino padrão: endereço, versão, autoria e datas, nunca o segredo | `queries.destinations.detail` (a consulta que `views.destination_page` decora) | `GET /destinations/{scope}` | `get_destination` | leitura | — | feita · tests_app/test_admin_api_destinations.py::test_get_destination_gives_the_total_key_the_whole_url_and_never_the_secret |
| `GET /admin/destinations/forms/{slug}` | O destino de um formulário (ou o que vale para ele, se não tem próprio) | `queries.destinations.detail`; `actions.destinations.target` (o escopo existe) | `GET /destinations/{scope}` | `get_destination` | leitura | — | feita · tests_app/test_admin_api_destinations.py::test_get_destination_gives_the_total_key_the_whole_url_and_never_the_secret |
| `POST /admin/destinations/default/url` | Cria o destino padrão (o segredo aparece só nesta resposta) ou muda o endereço dele | `actions.destinations.save` (a regra de produção e a transação) e `secret_already_shown` (o segredo já gerado por outro clique); a mensagem de cada lado é dele | `PUT /destinations/{scope}` | `save_destination` | total | externo | feita · tests_app/test_admin_api_destinations.py::test_save_destination_creates_with_a_secret_that_shows_once_and_never_in_log_audit_or_reads |
| `POST /admin/destinations/forms/{slug}/url` | Idem, para o destino de um formulário | `actions.destinations.save`, igual à anterior, mais `actions.destinations.target` (o escopo existe) | `PUT /destinations/{scope}` | `save_destination` | total | externo | feita · tests_app/test_admin_api_destinations.py::test_save_destination_creates_with_a_secret_that_shows_once_and_never_in_log_audit_or_reads |
| `POST /admin/destinations/default/secret` | Gera um segredo novo, mostrado uma vez; o antigo deixa de valer na hora | `actions.destinations.rotate` e `secret_already_shown`; o `confirm` é da página (o formulário) e da API (a marca) | `POST /destinations/{scope}/rotate-secret` | `rotate_destination_secret` | total | destrutiva, externo | feita · tests_app/test_admin_api_destinations.py::test_rotate_destination_secret_gives_a_new_secret_once_and_the_old_one_stops_at_once |
| `POST /admin/destinations/forms/{slug}/secret` | Idem, para o destino de um formulário | `actions.destinations.rotate`, igual à anterior, mais `actions.destinations.target` | `POST /destinations/{scope}/rotate-secret` | `rotate_destination_secret` | total | destrutiva, externo | feita · tests_app/test_admin_api_destinations.py::test_rotate_destination_secret_gives_a_new_secret_once_and_the_old_one_stops_at_once |
| `POST /admin/destinations/default/remove` | Remove o destino padrão; os formulários sem destino próprio deixam de entregar | `actions.destinations.remove` (devolve o destino como estava e quem entrega depois); o `confirm` é da página e da API | `DELETE /destinations/{scope}` | `remove_destination` | total | destrutiva, externo | feita · tests_app/test_admin_api_destinations.py::test_remove_destination_deletes_it_and_says_who_delivers_the_scope_now |
| `POST /admin/destinations/forms/{slug}/remove` | Remove o destino de um formulário (inclusive o de um slug que saiu do registro) | `actions.destinations.remove`, igual à anterior, mais `actions.destinations.target` (aceita o slug que saiu do registro) | `DELETE /destinations/{scope}` | `remove_destination` | total | destrutiva, externo | feita · tests_app/test_admin_api_destinations.py::test_remove_destination_deletes_it_and_says_who_delivers_the_scope_now |
| `POST /admin/destinations/default/test` | Manda um evento de teste (sem dado de lead) ao destino padrão e mostra a resposta | `destinations.send_test`; a API devolve o resultado como dados (o `Probe` e o que uma entrega faria com ele), e a página o decora com `views.probe_view` (rótulo) | `POST /destinations/{scope}/test-event` | `send_destination_test_event` | total | externo | feita · tests_app/test_admin_api_destinations.py::test_the_test_event_goes_to_the_receiver_signed_with_the_secret_and_carries_no_lead_data |
| `POST /admin/destinations/forms/{slug}/test` | Idem, para o destino que vale para o formulário | `destinations.send_test`, igual à anterior | `POST /destinations/{scope}/test-event` | `send_destination_test_event` | total | externo | feita · tests_app/test_admin_api_destinations.py::test_the_test_event_goes_to_the_receiver_signed_with_the_secret_and_carries_no_lead_data |

### Formulários: cadastro, situação e retenção

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/forms` | Lista os formulários: situação, versão publicada, rascunho pendente, dados a revisar e número de envios (na página, um link para os pedidos do formulário; na API, `list_jobs` com `form`) | `queries.forms` (a consulta que `views.form_list` decora) | `GET /forms` | `list_forms` | leitura | — | feita · tests_app/test_admin_api_forms.py::test_list_forms_is_an_envelope_by_slug_with_the_state_of_each_form |
| `GET /admin/forms/import` | Tela da importação (caixa de texto e arquivo) | — (só tela) | — | — | — | — | fora (B) |
| `POST /admin/forms/import` | Confere a definição e publica a versão 1, com confirmação dos dados de contato no prompt | `stored_forms.import_definition`; presa na rota: ler o arquivo ou o texto colado, "nada para importar" | `POST /forms/import` | `import_form` | total | externo | feita · tests_app/test_admin_api_drafts.py::test_import_form_publishes_version_1_and_audits_the_import |
| `GET /admin/forms/new` | Tela de criar um formulário do zero ou por cópia (`?from=`), com título e endereço sugeridos | `views.form_sources`, `views.copy_suggestion` (conveniência da tela) | — | — | — | — | fora (B) |
| `POST /admin/forms/new` | Cria o formulário sem versão publicada e o rascunho dele, do modelo inicial ou da cópia de outro | `drafts.create`; presa na rota: o mapa de problemas por campo | `POST /forms` | `create_form` | total | — | feita · tests_app/test_admin_api_drafts.py::test_create_form_makes_the_form_without_a_version_and_its_draft_from_the_starter |
| `GET /admin/forms/{slug}` | O formulário: situação, versão publicada, histórico, rascunho, dados a revisar, destino, retenção, integração e a leitura da versão | `queries.forms` (a consulta que `views.form_detail` decora; junta `stored_forms`, `drafts.load`, `data_notices`, `form_retention.periods_of` e a integração) | `GET /forms/{slug}` | `get_form` | leitura | — | feita · tests_app/test_admin_api_forms.py::test_get_form_gives_the_state_the_versions_and_the_draft |
| `POST /admin/forms/{slug}/activate` | Volta a receber envios (a página não pede confirmação; a API exige `confirm: true`, por ser `externo`) | `stored_forms.set_active` (o mesmo serviço; a API acrescenta o `confirm`) | `POST /forms/{slug}/activate` | `activate_form` | total | externo | feita · tests_app/test_admin_api_forms.py::test_activating_puts_the_form_back_on_the_air |
| `POST /admin/forms/{slug}/deactivate` | Tira o formulário do ar para as leads (a página diz que está indisponível; a API recusa envios) | `stored_forms.set_active` (o mesmo serviço; a página e a API pedem o `confirm` antes de chamá-lo) | `POST /forms/{slug}/deactivate` | `deactivate_form` | total | destrutiva, externo | feita · tests_app/test_admin_api_forms.py::test_deactivating_takes_the_form_off_the_air_for_the_leads |
| `GET /admin/forms/{slug}/retention` | Os prazos de retenção do formulário e os do ambiente | `queries.forms` (a consulta que `views.retention_page` decora), `form_retention.periods_of` | `GET /forms/{slug}/retention` | `get_form_retention` | leitura | — | feita · tests_app/test_admin_api_forms.py::test_get_form_retention_gives_the_periods_the_defaults_the_limits_and_the_version |
| `POST /admin/forms/{slug}/retention` | Grava os prazos; uma redução pede confirmação com a contagem do que a próxima retenção apagaria | `form_retention.save`, com `parse_days`, `parse_seen` e `deletion_sentence` no mesmo módulo e `queries.forms` (a contagem da redução); o laço que lê os dois prazos em texto é da página (a API recebe inteiros e valida de 1 ao teto do serviço) | `PUT /forms/{slug}/retention` | `save_form_retention` | total | destrutiva | feita · tests_app/test_admin_api_forms.py::test_a_reduction_with_the_count_it_asked_for_saves_and_audits_the_count |

### Versões

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/forms/{slug}/versions/{number}/export` | A definição de uma versão publicada, como o banco a guarda, num arquivo JSON | `stored_forms.stored_version` (a API devolve a definição sem passar pelo compilador) | `GET /forms/{slug}/versions/{number}` | `get_form_version` | leitura | — | feita · tests_app/test_admin_api_forms.py::test_get_form_version_gives_the_definition_exactly_as_the_database_keeps_it |
| `GET /admin/forms/{slug}/versions/compare` | Duas versões, ou uma e o rascunho, seção por seção | `actions.versions.compare` (a escolha padrão de "de" e "para", por `Draft.pending`, e a leitura de cada documento) e `changes.compare` | `GET /forms/{slug}/comparison` | `compare_form_versions` | leitura | — | feita · tests_app/test_admin_api_forms.py::test_compare_form_versions_reads_two_versions_section_by_section |
| `POST /admin/forms/{slug}/versions/{number}/revert` | Copia uma versão publicada para o rascunho, no lugar do que ele tinha | `drafts.revert` (a versão que não compila mais volta como `DefinitionError`, com os problemas); o `confirm` da página é a caixa dela e o da API é o `confirm: true` do corpo, da dependência de confirmação | `POST /forms/{slug}/versions/{number}/revert` | `revert_form_draft_to_version` | total | destrutiva | feita · tests_app/test_admin_api_publish.py::test_revert_form_draft_to_version_leaves_the_same_state_and_audit_as_the_page |

### Rascunho: editor

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/forms/{slug}/draft` | O editor: a definição do rascunho em campos, a pontuação salva e a situação do rascunho | `drafts.load` (a tela é `draft_pages.editor`) | `GET /forms/{slug}/draft` | `get_form_draft` | leitura | — | feita · tests_app/test_admin_api_drafts.py::test_get_form_draft_gives_the_whole_document_and_the_version_of_the_page |
| `POST /admin/forms/{slug}/draft` | Guarda o rascunho se ele compila, com a versão esperada; antes da primeira publicação, muda também o endereço | `drafts.save`, que recebe os textos do editor (`DraftTexts`), não o documento: falta `save_document`; presa na rota: montar o documento dos campos (`_posted`, `FormEdit`) | `PUT /forms/{slug}/draft` | `save_form_draft` | total | — | feita · tests_app/test_admin_api_drafts.py::test_save_form_draft_leaves_the_same_state_and_audit_as_the_fields_page |
| `POST /admin/forms/{slug}/draft/edit` | Uma operação do editor estruturado sobre o que a página mandou, sem gravar (ver [as 48](#operações-do-editor-48)) | `FormEdit.apply`, `PlanEdit`, `DataEdit`, `ScoringEdit`: presa na rota, e feita sobre o estado do formulário HTML | `PATCH /forms/{slug}/draft` | `patch_form_draft` | total | — | feita · tests_app/test_admin_api_drafts.py::test_patch_form_draft_edits_the_draft_and_answers_with_the_new_document |
| `GET /admin/forms/{slug}/draft/preview` | Moldura (um `iframe` de 375 px) para a prévia da página pública | `draft_pages.preview_page` (só tela) | — | — | — | — | fora (B) |
| `GET /admin/forms/{slug}/draft/preview/frame` | A página pública do rascunho inteira, como a lead a veria, a partir da etapa `etapa` | `actions.previews.public_page` (o contexto do template: as etapas, as condições e a etapa em que abre) e `render_public_page` (o HTML); a rota só lê o `?etapa=` (`draft_pages.step_of`) | `GET /forms/{slug}/draft/public-page` | `render_form_draft_public_page` | leitura | — | feita · tests_app/test_admin_api_previews.py::test_the_public_page_of_the_api_is_the_page_the_admin_shows_for_the_same_data |
| `GET /admin/forms/{slug}/draft/completion-preview` | Recibo de exemplo da conclusão salva, com botão externo desativado e texto escapado | `actions.previews.completion_preview` e `web.completion.render_completion`; não salva respostas, não navega e não chama LLM/PDF | `GET /forms/{slug}/draft/completion-preview` | `render_form_draft_completion_preview` | leitura | — | feita · tests_app/test_admin_completion.py::test_completion_preview_is_read_only_and_cannot_navigate_outside |

### Rascunho: PDF e prévias

**Como a prévia se escreve na API** (fase 7, referência técnica).
As três operações só leem (`leitura`, 5 fichas do limite de uso) e usam o plano e as respostas de exemplo
do rascunho, ou o plano do último teste: nenhum dado de lead. O PDF e a página de resultado aceitam um
corpo opcional com `pdf` (`html`, `css`, `scores`), `preview` e `source` (`example` ou `test`) que vale
**sem gravar**, campo a campo (o que a chamada deixa de fora é o do rascunho salvo; sem corpo, vale o
salvo); os nomes são os do documento (`pdf.html`, `pdf.css`, `pdf.scores`, `preview`), e o erro de um
template volta em `422` com esse caminho e a linha. **Mandar `pdf` ou `preview` é só do papel `total`**
(`403 forbidden_role` antes de compilar: o papel da operação segue `leitura`, e a `leitura` pré-visualiza o
rascunho salvo, sem corpo, com `{}` ou só com `source`). O plano do último teste que falta, ou que o modelo do
rascunho já não aceita, é `409 state_conflict`; um PDF que o adaptador não gera é `422` no campo `pdf`, com
a mensagem fixa dele. A resposta não é JSON: o PDF é `application/pdf` (`inline; filename="previa.pdf"`, o
mesmo da página) e as duas páginas são `text/html` (`inline`), o mesmo HTML que a página mostra, com um
`<base>` para a origem do app (`PUBLIC_BASE_URL`, para o CSS, o JavaScript e as imagens, que a página pede
pelo caminho; só vale se `PUBLIC_BASE_URL` for a origem da API) e uma política de conteúdo própria, no
cabeçalho e numa `<meta http-equiv>` (para a página salva em arquivo): `sandbox` sem `allow-same-origin` (o
documento não tem a origem do admin), só os scripts do app na página pública e nenhum na de resultado (nem o
que o operador escreveu na prévia). O PDF leva `frame-ancestors 'none'`. A etapa em que a página pública abre é `step`, a partir de 1 (a mais próxima, se passa
do fim). A moldura (`GET /admin/forms/{slug}/draft/preview`) segue sem operação (`fora (B)`).

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/forms/{slug}/draft/pdf` | O editor do PDF, da prévia da página de resultado e das notas, com os testes recentes e as imagens visíveis | `drafts.load`, `draft_tests.recent`, `form_assets.visible` (a tela é `draft_pages.pdf_editor`) | `GET /forms/{slug}/draft` | `get_form_draft` | leitura | — | feita · tests_app/test_admin_api_drafts.py::test_get_form_draft_has_the_pdf_and_the_preview_the_pdf_page_shows |
| `POST /admin/forms/{slug}/draft/pdf` | Guarda o PDF, a prévia e as notas, se compilam e o PDF só pede arquivos do app | `drafts.save_templates`; presa na rota: ler os campos `pdf_html`, `pdf_css`, `preview`, `pdf_scores` | `PUT /forms/{slug}/draft` | `save_form_draft` | total | — | feita · tests_app/test_admin_api_drafts.py::test_save_form_draft_leaves_the_same_state_and_audit_as_the_pdf_page |
| `POST /admin/forms/{slug}/draft/pdf/preview` | O PDF do rascunho como está na página (salvo ou não), com o plano de exemplo ou o do último teste; não grava | `actions.previews.template_preview` (o rascunho com o PDF e a prévia que a chamada manda, o plano e as imagens) e `render_pdf` (o PDF do adaptador do pedido, `PdfFailed` se ele não gera); os erros são dados (`DefinitionError`, `PreviewUnavailable`) e a rota só os põe no quadro (`_frame_error`) | `POST /forms/{slug}/draft/pdf-preview` | `render_form_draft_pdf_preview` | leitura | — | feita · tests_app/test_admin_api_previews.py::test_the_pdf_of_the_api_is_the_pdf_the_page_shows_for_the_same_data |
| `POST /admin/forms/{slug}/draft/pdf/result` | A página de resultado do rascunho como a lead a vê, com a prévia salva ou não; não grava | `actions.previews.template_preview` e `render_result` (o HTML da prévia na moldura da página de resultado; a prévia que não renderiza volta como `DefinitionError`, com a linha); a rota só põe o erro no quadro | `POST /forms/{slug}/draft/result-preview` | `render_form_draft_result_preview` | leitura | — | feita · tests_app/test_admin_api_previews.py::test_the_result_page_of_the_api_is_the_page_the_admin_shows_for_the_same_data |

### Rascunho: testes

**Como o teste se escreve na API** (fase 3.2, referência técnica).
As respostas chegam em `answers`, como **objeto** (ausente: as de exemplo do rascunho; `drafts.assemble`,
`drafts.score` e `draft_tests.start` passaram a aceitar o objeto além do texto da página, e as duas formas
caem na mesma conferência). A data simulada é `simulated_date` (o campo `date` da página; um problema da
data volta com esse nome), e a pontuação não a tem, porque só lê as respostas. Montar o prompt, pontuar e
listar não gravam nem chamam o LLM; `list_form_draft_tests` devolve os testes no envelope das listas da
ADR (`items` e `next_cursor`, sempre nulo: são poucos e todos cabem; sem `limit` nem `cursor`) e, quando o
prompt não monta, `prompt` nulo e `problems`, em vez de falhar, porque a página também lista os testes
nesse caso. Uma resposta a uma pergunta que o formulário não tem é `422` no campo `answers`, sem citar a
chave (a chave é texto de quem chama; o texto da página segue apontando `answers.<chave>`). A recusa do teste é
`DraftTestRefused`, e `.paused` diz qual: `409 state_conflict` (um teste em andamento) ou `429 test_paused`
com `Retry-After` (a pausa). Um disparo que o engine não recebe não é erro (`launched: false`, e o teste
termina como `not_started`). O que o teste diz à publicação é `draft_tests.gate_of`, um código
(`current`, `unlocks`, `stale`) que a página traduz em frase. Um `test_id` que não é um UUID é `404 not_found` (não `422`), como
na página, e o contrato diz isso. **Os dados de um teste são da operadora (respostas fictícias), não de uma lead**: não levam
`x-lead-data`, o papel `leitura` os lê, e o prompt, o plano e as respostas nunca vão para log, mensagem de
erro nem auditoria. O PDF de um teste custa 5 fichas, como todo download.

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/forms/{slug}/draft/tests` | A página do teste: as respostas de exemplo, o prompt montado e os testes recentes | `actions.draft_tests.testing`: o prompt montado com as respostas de exemplo (ou os problemas, que não derrubam a lista) e `draft_tests.recent` | `GET /forms/{slug}/draft/tests` | `list_form_draft_tests` | leitura | — | feita · tests_app/test_admin_api_draft_tests.py::test_list_form_draft_tests_lists_the_same_tests_in_the_same_order_as_the_page |
| `POST /admin/forms/{slug}/draft/prompt` | O prompt montado com as respostas e a data simulada; não grava e não chama o LLM | `actions.draft_tests.testing`: `drafts.assemble` com as respostas em texto (a página) ou em objeto (a API) | `POST /forms/{slug}/draft/prompt` | `assemble_form_draft_prompt` | leitura | — | feita · tests_app/test_admin_api_draft_tests.py::test_assemble_form_draft_prompt_gives_the_prompt_the_page_shows_with_the_example_answers |
| `POST /admin/forms/{slug}/draft/score` | A pontuação do rascunho com as respostas: cada nota, a prioridade, a persona e a regra que decidiu | `actions.draft_tests.testing(scoring=True)`: `drafts.score` com as respostas em texto ou em objeto | `POST /forms/{slug}/draft/score` | `score_form_draft` | leitura | — | feita · tests_app/test_admin_api_draft_tests.py::test_score_form_draft_gives_the_same_scores_the_page_shows |
| `POST /admin/forms/{slug}/draft/tests` | Põe um teste na fila (uma chamada paga ao LLM) e dispara a task `draft_test`; recusa se há um em andamento ou na pausa | `actions.draft_tests.start`: `draft_tests.start`, o disparo (`launching.launch_draft_test`, com o `app.state.test_trigger`) e `draft_tests.not_started` se o engine não recebe; a recusa (`DraftTestRefused`) em 409 ou 429 com `Retry-After`, conforme `DraftTestRefused.paused` | `POST /forms/{slug}/draft/tests` | `start_form_draft_test` | total | externo | feita · tests_app/test_admin_api_draft_tests.py::test_start_form_draft_test_leaves_the_same_state_audit_and_launch_as_the_page |
| `GET /admin/forms/{slug}/draft/tests/{test_id}` | O resultado de um teste: plano, PDF, tempos, tokens e rodadas de reparo | `actions.draft_tests.find`: o id do caminho em UUID, o teste do formulário e o estado de agora (`interrupted` depois do prazo); `draft_tests.gate_of` diz o que ele vale para a publicação | `GET /forms/{slug}/draft/tests/{test_id}` | `get_form_draft_test` | leitura | — | feita · tests_app/test_admin_api_draft_tests.py::test_get_form_draft_test_of_a_test_that_finished_brings_the_plan_the_times_and_the_tokens |
| `GET /admin/forms/{slug}/draft/tests/{test_id}/status` | O trecho que o HTMX troca enquanto o teste roda (286 e `HX-Refresh` ao terminar) | `actions.draft_tests.find` | `GET /forms/{slug}/draft/tests/{test_id}` | `get_form_draft_test` | leitura | — | feita · tests_app/test_admin_api_draft_tests.py::test_get_form_draft_test_gives_what_the_status_fragment_of_the_page_says |
| `GET /admin/forms/{slug}/draft/tests/{test_id}/pdf` | O PDF que o teste gerou, como anexo | `actions.draft_tests.pdf`: a busca do teste, os bytes (`draft_tests.pdf_of`) e o nome `teste-<slug>-<AAAAMMDD-HHMM>.pdf` | `GET /forms/{slug}/draft/tests/{test_id}/pdf` | `download_form_draft_test_pdf` | leitura | — | feita · tests_app/test_admin_api_draft_tests.py::test_download_form_draft_test_pdf_gives_the_file_the_page_downloads |

### Rascunho: publicação

**A confirmação dos dados de contato no prompt** (decidida na fase 3.1, referência técnica,
e igual em `import_form` e `publish_form_draft`). Quando o prompt cita o nome, o e-mail ou o telefone
da lead, o serviço pede a confirmação de quem publica (`contact_confirmed`; a página manda
`confirm_contact`). Na API é **outro campo além do `confirm`**: `confirm_contact: true` no corpo, com o
mesmo nome da página, e a falta dele é `422 confirmation_required`, o código da confirmação que falta
(o mesmo da retenção, que pede a contagem em `seen`), com os dados citados em `detail` ("o nome e o
e-mail") e nada gravado. Não é `validation_failed`: a definição é válida, e falta a pessoa dizer que
sabe o que o prompt manda ao provedor do modelo; e o MCP, que já pergunta à pessoa a cada
`confirmation_required`, a faz com o texto do `detail`. O campo só conta quando o prompt cita contato (em
outro caso é ignorado), e a conferência vem depois da validação da definição: uma definição que não
compila é `validation_failed` antes de qualquer pergunta. A conferência é a do serviço
(`stored_forms.ContactInPrompt`), e a API não a reimplementa.

**A simulação, a recusa e o teste** (decididos na fase 3.3, referência técnica; ver as
["Decisões da fase 3.3"](../adr/0015-api-de-admin.md#decisões-da-fase-33) da ADR). `publish_form_draft` com
`dry_run: true` roda as mesmas conferências, o PDF de exemplo no adaptador incluído, e responde `200` com o
que faria e o que falta (`review.missing` e `review.problems`, e `ready`), sem gravar, auditar nem exigir o
`confirm` (nem o `confirm_contact`: a resposta traz os dados citados e diz o campo a mandar); só a simulação
e a publicação renderizam o PDF (5 fichas), e a revisão (`GET`) não. A publicação recusada é `422
validation_failed` com as duas listas de `PublishRefused`: `reasons` (o que falta) e `errors` (os problemas,
com o caminho e a linha, e a nota que falta, no campo `note`); só a confirmação dos dados de contato que falta, com o resto em dia, é `422
confirmation_required`. O teste que a publicação pede é a regra do serviço (`Review.test_status`, que
concorda com `draft_tests.gate_of`), e a revisão o mostra em `test.status`: `not_needed`, `covered` ou
`missing`.

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/forms/{slug}/draft/publish` | O que a publicação faria (mudanças por seção, o que vai ao LLM, dados de contato no prompt) e o que falta para ela | `drafts.review`, sem o PDF de exemplo; o que a página decidia sozinha (o teste que a publicação pede e os valores de opção que ela deixa de gravar) é do serviço: `Review.test_status`, `Review.blockers` e `Review.dropped_values` | `GET /forms/{slug}/draft/review` | `review_form_draft_publication` | leitura | — | feita · tests_app/test_admin_api_publish.py::test_review_form_draft_publication_says_what_the_publication_would_do_and_what_is_missing |
| `POST /admin/forms/{slug}/draft/publish` | Publica o rascunho como a versão seguinte, com a nota; a versão vale já para os envios novos | `drafts.publish`; com `dry_run=True` roda tudo, o PDF de exemplo incluído, e devolve a `Simulation` sem gravar nem auditar; a recusa (`PublishRefused`) em `422` com as duas listas (`reasons` e `problems`), e só a confirmação dos dados de contato em `422 confirmation_required` (`only_contact_missing`); o `confirm` que o `dry_run` dispensa é da dependência de confirmação (`confirm_unless`) | `POST /forms/{slug}/draft/publish` | `publish_form_draft` | total | externo | feita · tests_app/test_admin_api_publish.py::test_publish_form_draft_leaves_the_same_state_and_audit_as_the_page |

### Integração com o Baserow

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/forms/{slug}/baserow` | A integração: conexão (sem o token), os campos da tabela lidos agora no Baserow com a sugestão de fonte, o relatório do mapeamento, as contagens e as sincronizações que falharam | `actions.integrations.read_detail` (a leitura da tabela ao vivo, `read_fields`, e o relatório) sobre `queries.integrations` (a consulta sem rótulo que `integration_views` decora) | `GET /forms/{slug}/integrations/baserow` | `get_form_integration` | leitura | — | feita · tests_app/test_admin_api_integrations.py::test_get_form_integration_reads_the_live_table_and_suggests_a_source_for_each_field |
| `POST /admin/forms/{slug}/baserow/connect` | Conecta o formulário a uma tabela (ou muda a conexão), depois de ler a tabela com o token; token em branco mantém o guardado | `actions.integrations.connect` (o endereço, o número da tabela, o token obrigatório na criação e a leitura de teste da tabela) e `integration_config.connect` | `PUT /forms/{slug}/integrations/baserow/connection` | `connect_form_integration` | total | destrutiva | feita · tests_app/test_admin_api_integrations.py::test_connect_form_integration_reads_the_table_stores_the_connection_and_never_gives_the_token |
| `POST /admin/forms/{slug}/baserow/mapping` | Guarda o mapeamento (fonte de cada campo da tabela), conferido contra a tabela de agora; desativa a integração se o relatório tem pendências | `actions.integrations.save_mapping` (a leitura ao vivo, `check_entries`, `build_report` e a decisão de desativar) e `integration_config.save_mapping`; a página só lê os campos `source_<id>` do formulário | `PUT /forms/{slug}/integrations/baserow/mapping` | `save_form_integration_mapping` | total | — | feita · tests_app/test_admin_api_integrations.py::test_save_form_integration_mapping_stores_it_checked_against_the_live_table_and_audits_it |
| `POST /admin/forms/{slug}/baserow/activate` | Liga a integração se o relatório está limpo; vale para os pedidos que ficarem prontos daqui em diante | `actions.integrations.activate` (a versão esperada, a leitura ao vivo, `build_report` limpo) e `integration_config.set_active` | `POST /forms/{slug}/integrations/baserow/activate` | `activate_form_integration` | total | externo | feita · tests_app/test_admin_api_integration_actions.py::test_activate_form_integration_reads_the_live_table_once_turns_it_on_and_audits_it |
| `POST /admin/forms/{slug}/baserow/deactivate` | Desliga a integração; as sincronizações que repetiam esperam | `actions.integrations.deactivate` e `integration_config.set_active` | `POST /forms/{slug}/integrations/baserow/deactivate` | `deactivate_form_integration` | total | — | feita · tests_app/test_admin_api_integration_actions.py::test_deactivate_form_integration_turns_it_off_without_a_confirm_or_a_call_to_baserow |
| `POST /admin/forms/{slug}/baserow/remove` | Remove a integração: apaga o token e o estado das sincronizações (as linhas da tabela ficam) | `actions.integrations.remove` (que diz o estado das sincronizações que saem) e `integration_config.remove`; o `confirm` é a caixa da página e o `confirm=true` da query na API | `DELETE /forms/{slug}/integrations/baserow` | `remove_form_integration` | total | destrutiva | feita · tests_app/test_admin_api_integration_actions.py::test_remove_form_integration_deletes_the_token_and_the_sync_state_but_not_the_table_rows |
| `POST /admin/forms/{slug}/baserow/test` | Escreve uma linha sintética (a lead de exemplo) na tabela, pelo caminho de um pedido de verdade | `actions.integrations.send_test` sobre `integration_push.send_sample`, que devolve o `SampleResult` como dado; a frase da tela é `integration_views.sample_view`, só da página | `POST /forms/{slug}/integrations/baserow/test` | `send_form_integration_test` | total | externo | feita · tests_app/test_admin_api_integration_actions.py::test_send_form_integration_test_writes_the_sample_row_and_the_second_call_updates_it |
| `POST /admin/forms/{slug}/baserow/resend-failed` | Reenvia de uma vez as sincronizações que falharam (até 200) | `actions.integrations.resend_failed` sobre `integration_syncs.resend_failed`, com o limite de 200 (`launching.RESEND_FAILED_LIMIT`); o disparo em segundo plano é de `launching`: a página usa `launch_syncs_later` e a API, `launch_syncs`, que espera só a primeira | `POST /forms/{slug}/integrations/baserow/resend-failed` | `resend_failed_form_integration_syncs` | total | externo | feita · tests_app/test_admin_api_integration_actions.py::test_resend_failed_form_integration_syncs_puts_them_back_in_the_queue_and_launches_them |

### Imagens dos formulários

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/assets` | Lista as imagens da biblioteca geral e as de cada formulário | `form_assets.listing` (a tela é `asset_pages.assets_page`); a API acrescenta onde cada uma é usada, numa leitura só (`form_assets.uses_of`) | `GET /assets` | `list_assets` | leitura | — | feita · tests_app/test_admin_api_assets.py::test_list_assets_is_an_envelope_with_the_library_first_and_then_each_form_by_name |
| `POST /admin/assets` | Envia uma imagem nova (multipart): otimiza, guarda no storage e mostra o que a otimização fez | `form_assets.upload`, com `actions.assets.content_of` (o arquivo até o limite mais um byte), `form_assets.Optimization` (os números da otimização, que a página escreve em `asset_pages.optimization`) e `form_assets.storage_failure` (a frase da falha do storage: a página a mostra em `502`, a API a devolve em `502 upstream_failed`) | `POST /assets` | `upload_asset` | total | — | feita · tests_app/test_admin_api_assets.py::test_upload_asset_optimizes_stores_and_answers_with_the_image_and_the_numbers |
| `GET /admin/assets/{asset_id}` | A imagem: escopo, tipo, tamanhos antes e depois, onde é usada | `form_assets.find`, `form_assets.uses` | `GET /assets/{asset_id}` | `get_asset` | leitura | — | feita · tests_app/test_admin_api_assets.py::test_get_asset_gives_the_image_to_a_read_key |
| `POST /admin/assets/{asset_id}/replace` | Troca o conteúdo mantendo o nome; as versões publicadas seguem com o antigo | `form_assets.replace`, com as mesmas `content_of`, `Optimization` e `storage_failure` do envio | `PUT /assets/{asset_id}/content` | `replace_asset` | total | — | feita · tests_app/test_admin_api_assets.py::test_replace_asset_changes_the_content_and_keeps_the_name_and_the_id |
| `POST /admin/assets/{asset_id}/remove` | Remove a imagem; uma versão publicada que a usa recusa | `form_assets.remove`, com `AssetInUse.message` (a frase que diz quais versões a usam: a página a mostra, a API a devolve em `409 state_conflict`); o `confirm` é a caixa da página e o `confirm=true` da query na API | `DELETE /assets/{asset_id}` | `remove_asset` | total | destrutiva | feita · tests_app/test_admin_api_assets.py::test_remove_asset_deletes_the_image_and_its_file_and_answers_with_what_went |

### Chaves de API

As chaves têm nome e papel, ficam guardadas só como hash e a chave inteira aparece uma única vez, na
resposta da criação ([ADR 0015](../adr/0015-api-de-admin.md#autenticação-e-chaves)). A página existe
antes de uma chave poder fazer qualquer coisa (fase 0.1); as operações da API chegaram na fase 0.2
(referência técnica). Como
a chave `total` cria outras chaves `total`, `create_api_key` e `revoke_api_key` pedem `total`. Pela
API, criar uma chave é uma ação marcada: `create_api_key` leva `x-external-effect` e exige
`confirm: true` em toda chamada, de qualquer papel (uma chave `total` num agente poderia criar outra;
a página não pede, porque a operadora logada vê o que cria). A exigência nasceu com a rota, na fase 0.2.

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/api-keys` | Lista as chaves (nome, papel, autoria, validade, último uso, situação; nunca o segredo) e traz o formulário de criar, com a validade de 90 dias preenchida | `keys.listing` (`app/admin/api/keys.py`) | `GET /api-keys` | `list_api_keys` | leitura | — | feita · tests_app/test_admin_api_keys_api.py::test_the_list_is_an_envelope_with_every_key_the_newest_first_the_revoked_too |
| `POST /admin/api-keys` | Cria a chave (nome, papel, validade opcional); a chave inteira aparece só nesta resposta, com `Cache-Control: no-store` | `keys.create` (a validade em dias é só da tela: a API manda `expires_at`) | `POST /api-keys` | `create_api_key` | total | externo | feita · tests_app/test_admin_api_keys_api.py::test_creating_and_revoking_by_the_api_leave_the_same_state_and_audit_as_the_page |
| `POST /admin/api-keys/{key_id}/revoke` | Revoga a chave, com confirmação; vale na próxima chamada. Na API, revoga também a chave que chama | `keys.revoke`; presa na rota: `confirm` | `POST /api-keys/{key_id}/revoke` | `revoke_api_key` | total | destrutiva | feita · tests_app/test_admin_api_keys_api.py::test_creating_and_revoking_by_the_api_leave_the_same_state_and_audit_as_the_page |

### Configurar o MCP

A tela `/admin/mcp` leva a operadora do zero ao MCP funcionando (escolher ou criar a chave, instalar,
montar o comando do Claude Code ou o bloco do Claude Desktop, conferir a chave). A criação da chave ali é
a mesma de `/admin/api-keys` (`app/admin/api_key_routes.py::create_from_form`: a regra, a auditoria e o
log são os mesmos), então tem a mesma operação da API. A tela em si é `fora (D)`.

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/mcp` | Tela de configuração do MCP: lista as chaves que valem (nome e papel), monta o comando e o JSON com o endereço deste admin e conta as ferramentas de cada papel | `keys.listing`; `mcp_pages` (texto de tela) | — | — | — | — | fora (D) |
| `POST /admin/mcp/keys` | Cria a chave pela tela do MCP (nome, papel, validade opcional); o segredo aparece só nesta resposta, já nos comandos, com `Cache-Control: no-store` | `keys.create` (o mesmo de `POST /admin/api-keys`, por `create_from_form`) | `POST /api-keys` | `create_api_key` | total | externo | feita · tests_app/test_admin_api_keys_api.py::test_creating_and_revoking_by_the_api_leave_the_same_state_and_audit_as_the_page |

## Operações do editor (48)

`POST /admin/forms/{slug}/draft/edit` recebe o formulário inteiro da página e um campo `op` (`add-question:1`,
`up-option:0:2:1`...), aplica a operação ao estado dele e devolve a página redesenhada: **nada é
gravado**, e quem grava é o "Salvar". A API não repete cada operação: o documento do rascunho é o
estado, `PUT /forms/{slug}/draft` o troca por inteiro e `PATCH /forms/{slug}/draft` o edita com
[JSON Patch (RFC 6902)](https://www.rfc-editor.org/rfc/rfc6902), numa transação só, conferindo o
resultado como o salvar confere. A tabela diz como cada operação se escreve nele (`i`, `j`, `k` são
os índices que a operação leva; os caminhos são os do documento, em `FormDocument`). O teste de
cobertura lê as operações de cada editor e cobra uma linha aqui para cada uma.

As seis operações `refresh` só redesenham a tela depois de o tipo ou o operador mudar (abrem os campos
do tipo novo, ajustam o valor da regra): quem escreve o documento já manda o valor na forma certa, e a
compilação confere.

**O que a tabela resume e os casos completam.** Cada linha é o patch mínimo; o editor ainda recalcula o
que depende do que mudou, e num patch isso é uma operação a mais no mesmo patch: a identidade que
apontava para uma pergunta que sai, a resposta de exemplo de uma pergunta (ou da opção marcada) que sai
ou que entra obrigatória, e o plano de exemplo (`examples.plan`), que o editor deixa ao JSON do resto
da definição. O que a operação de adicionar inclui é um campo **preenchido** (o editor o inclui em
branco e a operadora o preenche em seguida), porque o `PATCH` confere o documento inteiro antes de
gravar. Os casos completos, com os índices, os valores e esses acompanhamentos, estão em
`tests_app/admin_api_editor_cases.py`, e `test_every_editor_operation_has_a_patch_that_makes_the_same_document`
prova que cada patch deixa o mesmo documento que o editor (`FormEdit.apply`, e o `PlanEdit`, o `DataEdit`
e o `ScoringEdit` que ele carrega, seguidos de `screen()`), na forma normal; ele lê esta tabela e reprova
se uma operação do editor não tem caso. O teste de HTTP manda cada patch **completo** (o caso, mais o que o
documento precisa para compilar: o prompt sem a linha que citava a pergunta que sai, os mínimos de dois
níveis trocados) e espera `200` nos 53 casos; o patch sem o que falta é `422` com o caminho do campo, e
nada é gravado.

### Etapas, perguntas e opções (`FormEdit`, 19)

| Operação | O que faz | Equivalente no documento | Status |
| --- | --- | --- | --- |
| `add-step` | Inclui uma etapa com uma pergunta em branco | `add /steps/-` (uma pergunta obrigatória precisa de `add /examples/answers/<nome>` no mesmo patch) | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `remove-step:i` | Remove a etapa e tira a identidade que apontava para as perguntas dela | `remove /steps/{i}`, e no mesmo patch `remove /identity/<papel>` do papel que apontava para uma pergunta dela e `remove /examples/answers/<nome>` da resposta de exemplo de cada pergunta que sai | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `up-step:i` | Sobe a etapa | `move` de `/steps/{i}` para `/steps/{i-1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `down-step:i` | Desce a etapa | `move` de `/steps/{i}` para `/steps/{i+1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `add-question:i` | Inclui uma pergunta ao fim da etapa | `add /steps/{i}/questions/-` (uma pergunta obrigatória precisa de `add /examples/answers/<nome>` no mesmo patch) | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `remove-question:i:j` | Remove a pergunta | `remove /steps/{i}/questions/{j}` (e `remove /examples/answers/<nome>`, se ela tinha resposta de exemplo) | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `up-question:i:j` | Sobe a pergunta; no topo, vai para o fim da etapa de cima | `move` para `/steps/{i}/questions/{j-1}` ou `/steps/{i-1}/questions/-` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `down-question:i:j` | Desce a pergunta; no fim, vai para o começo da etapa de baixo | `move` para `/steps/{i}/questions/{j+1}` ou `/steps/{i+1}/questions/0` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `refresh-question:i:j` | Redesenha a pergunta depois de mudar o tipo | — | fora (C) |
| `add-option:i:j` | Inclui uma opção ao fim da pergunta | `add /steps/{i}/questions/{j}/options/-` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `remove-option:i:j:k` | Remove a opção | `remove /steps/{i}/questions/{j}/options/{k}` (e a resposta de exemplo da pergunta, se a opção era a marcada) | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `up-option:i:j:k` | Sobe a opção | `move` para `.../options/{k-1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `down-option:i:j:k` | Desce a opção | `move` para `.../options/{k+1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `add-condition:i:j[:k]` | Dá uma condição à pergunta (ou à opção `k`), com uma regra em branco | `add .../show_when` (`{"mode": ..., "rules": [...]}`) | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `remove-condition:i:j[:k]` | Tira a condição | `remove .../show_when` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `add-rule:i:j[:k]` | Inclui uma regra na condição | `add .../show_when/rules/-` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `remove-rule:i:j[:k]:r` | Remove a regra; a última leva a condição junto | `remove .../show_when/rules/{r}` (ou `.../show_when`, se era a última) | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `refresh-rule:i:j[:k]:r` | Ajusta a regra à pergunta que ela passou a olhar | — | fora (C) |
| `refresh-operator:i:j[:k]:r` | Dá ao valor da regra a forma do operador novo | — | fora (C) |

### Modelo do plano (`PlanEdit`, 7)

| Operação | O que faz | Equivalente no documento | Status |
| --- | --- | --- | --- |
| `plan-add-field:<endereço>` | Inclui um campo na raiz do plano ou num grupo | `add /plan/fields/-` (ou `.../fields/-` no grupo), e `add /examples/plan/<nome>` no mesmo patch | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `plan-remove:<endereço>` | Remove o campo | `remove /plan/fields/{n}` (aninhado, pelo caminho), e `remove /examples/plan/<nome>` no mesmo patch | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `plan-up:<endereço>` | Sobe o campo entre os irmãos | `move` para o índice anterior | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `plan-down:<endereço>` | Desce o campo | `move` para o índice seguinte | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `plan-refresh:<endereço>` | Redesenha o campo depois de mudar o tipo | — | fora (C) |
| `plan-add-rule` | Inclui uma regra de texto proibido | `add /plan/forbidden/-` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `plan-remove-rule:n` | Remove a regra | `remove /plan/forbidden/{n}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |

O rótulo de um campo (`label`, em `PlanNodeDoc`) não é uma operação: é uma chave do próprio campo, que se
escreve com `add` ou `replace` em `/plan/fields/{n}/label` (ou no `PUT` do documento). Só a tela do pedido o
lê, no lugar do nome do campo; ele não vai ao LLM, ao plano gravado, ao webhook nem ao PDF, e mudá-lo não
pede o teste do rascunho.

### Dados do formulário (`DataEdit`, 7)

| Operação | O que faz | Equivalente no documento | Status |
| --- | --- | --- | --- |
| `data-add` | Inclui um bloco de dados | `add /data/{nome}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `data-remove:i` | Remove o bloco | `remove /data/{nome}` (`i` é a posição do bloco na ordem dos nomes) | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `data-refresh:i` | Redesenha o bloco depois de mudar o tipo | — | fora (C) |
| `data-add-window:i` | Inclui uma janela de datas no bloco | `add /data/{nome}/windows/-` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `data-remove-window:i:k` | Remove a janela | `remove /data/{nome}/windows/{k}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `data-up-window:i:k` | Sobe a janela | `move` para `.../windows/{k-1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `data-down-window:i:k` | Desce a janela | `move` para `.../windows/{k+1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |

### Pontuação (`ScoringEdit`, 15)

| Operação | O que faz | Equivalente no documento | Status |
| --- | --- | --- | --- |
| `scoring-add-score` | Inclui uma nota | `add /scoring/scores/-` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-remove-score:i` | Remove a nota | `remove /scoring/scores/{i}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-up-score:i` | Sobe a nota | `move` para `/scoring/scores/{i-1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-down-score:i` | Desce a nota | `move` para `/scoring/scores/{i+1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-add-bands:i` | Dá faixas à nota | `add /scoring/scores/{i}/bands` (`key`, `label` e ao menos um nível) | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-remove-bands:i` | Tira as faixas | `remove /scoring/scores/{i}/bands` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-add-level:i` | Inclui um nível na faixa, antes do último, que vale para o resto | `add /scoring/scores/{i}/bands/levels/{n-1}` (`n` níveis; o último fica sem `min`) | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-remove-level:i:k` | Remove o nível | `remove /scoring/scores/{i}/bands/levels/{k}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-up-level:i:k` | Sobe o nível | `move` para `.../levels/{k-1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-down-level:i:k` | Desce o nível | `move` para `.../levels/{k+1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-add-rule:<lista>` | Inclui uma regra de prioridade ou de persona, antes do «senão» (a última regra, sem condição) | `add /scoring/{lista}/{n-1}` se a última regra não tem condição, senão `add /scoring/{lista}/-` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-remove-rule:<lista>:i` | Remove a regra | `remove /scoring/{lista}/{i}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-up-rule:<lista>:i` | Sobe a regra (a ordem decide qual vale) | `move` para `/scoring/{lista}/{i-1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-down-rule:<lista>:i` | Desce a regra | `move` para `/scoring/{lista}/{i+1}` | feita · tests_app/test_admin_api_drafts.py::test_every_editor_operation_has_a_patch_that_makes_the_same_document |
| `scoring-refresh-rule:<lista>:i` | Redesenha a regra depois de mudar o que ela olha | — | fora (C) |

## Operações novas (sem rota no admin)

Estas duas operações não vêm de uma ação que o admin tem: `get_current_api_key` diz quem está chamando
(decisão 1 da ADR), e `list_audit_log` lê a auditoria, que o admin só mostra, por pedido, na linha do
tempo. As três operações das chaves (`list_api_keys`, `create_api_key` e `revoke_api_key`) vêm da
página delas e estão em ["Chaves de API"](#chaves-de-api).

| `operation_id` | Operação da API | O que faz | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- |
| `get_current_api_key` | `GET /me` | Quem está chamando: nome da chave, papel e validade; o primeiro teste de uma chave nova | leitura | — | feita · tests_app/test_admin_api_keys_api.py::test_me_says_who_is_calling_without_the_secret |
| `list_audit_log` | `GET /audit-log` | Lê a auditoria (autor, ação, alvo, detalhes), filtrada por alvo, autor ou ação | leitura | — | feita · tests_app/test_admin_api_audit.py::test_list_audit_log_answers_an_envelope_of_the_newest_entries_first |

**Como a auditoria se lê na API** (fase 8, referência técnica).
`list_audit_log` lê a tabela `audit_log` inteira, da entrada mais nova para a mais antiga, com os filtros
`target_type`, `target_id`, `actor` e `action` (igualdade exata, somados) e o cursor de sempre, que aqui é
`(at, id)`: o `id` desempata as entradas do mesmo instante, e uma entrada que chega durante a leitura não
pula nem repete nenhuma. Os campos são os da tabela (a coluna `at` mantém o nome), e **os `details` saem
por uma lista de permissão de chaves** (`PUBLIC_DETAIL_KEYS`, em `app/admin/queries/audit.py`), que filtra
pelo nome e não olha o conteúdo: o valor sob uma chave permitida sai como foi escrito. A tabela é escrita
por vários serviços, e dois textos que ela guarda não saem: o `reason` de um reprocessamento, que a
operadora digita e pode citar a lead (a `total` o lê no `lead_data.notes` de `get_job`), e o `error` do
teste de uma integração, o texto de erro de fora, que o projeto trata como só do `total`. Por isso a
operação é de `leitura`, sem a marca `lead`. Os serviços de hoje não gravam o segredo, o hash de uma chave
nem o caminho da URL de um destino. O `target_id` é o id que a auditoria guarda: o id interno do pedido e
do formulário não é o código nem o `slug` que o resto da API usa (o `slug` vem em `details.form_slug`).
Ler a auditoria não a grava: só as escritas são auditadas (decisão 2 da revisão da ADR).

## O que não entra na API e por quê

- **(A) Login por senha, sessão e saída** (3 rotas). A API autentica por chave a cada chamada: não há
  sessão, cookie, token CSRF nem bloqueio de login por IP a replicar (a chave errada tem o bloqueio
  próprio, o mesmo mecanismo). Entrar com a senha continua sendo só pelo navegador.
- **(B) Telas sem dado nem ação** (3 rotas): a tela da importação (`GET /admin/forms/import`), a de
  criar um formulário (`GET /admin/forms/new`) e a moldura da prévia da página pública
  (`GET /admin/forms/{slug}/draft/preview`). A primeira e a segunda são formulários vazios (as
  sugestões de título e de endereço da cópia são conveniência de digitação; quem cria pela API manda
  o que quer, e a lista dos formulários de onde copiar é `list_forms`). A terceira é só o `iframe`
  que aponta para `.../preview/frame`, que tem a operação dele.
- **(C) Redesenho do editor** (6 operações dentro de `draft/edit`). `refresh-question`,
  `refresh-rule`, `refresh-operator`, `plan-refresh`, `data-refresh` e `scoring-refresh-rule` só
  refazem a tela depois de uma troca de tipo ou de operador. O documento não guarda o estado da tela.
- **(D) Tela de configurar o MCP** (1 rota): `GET /admin/mcp` monta o comando e o JSON do cliente a
  partir do endereço do admin e da lista de chaves (`list_api_keys` já dá o nome e o papel), e conta as
  ferramentas pelo mesmo catálogo do MCP. É ajuda de tela, não uma ação do admin que a API precise
  repetir: o MCP é um cliente da API, e o que ele faz está descrito no `openapi.json` (a seção «MCP» da
  descrição, que aponta para a tela).

Também fica de fora o que o admin **não** faz hoje: apagar um formulário, revogar o link de uma lead
e apagar o pedido de uma titular antes do prazo ([SECURITY.md](../../SECURITY.md#limites-conhecidos)).
A API não ganha essas ações na v1: cobertura total é o que o painel faz, e não mais poder do que ele
tem. Se o dono quiser uma delas, entra primeiro no admin, com a operação da API junto.

## Como o teste lê este arquivo

O teste de cobertura (`tests_app/test_admin_api_coverage.py`, fase 0 do camada de serviços) lê as
tabelas da seção "Inventário por recurso", as dos editores e a das operações novas (a dos totais ele
não lê: os números dela são para quem lê) e cobra:

1. Toda rota registrada em `/admin` (menos `/admin/api/v1`) tem uma linha, e toda linha tem uma
   rota: o teste enumera com `fastapi.routing.iter_route_contexts(app.routes)` (que desce nos
   roteadores incluídos; `app.routes` traz cada `include_router` como um item só) as que começam por
   `/admin` e não por `/admin/api/v1`, com cada método menos `HEAD` e `OPTIONS`, e cruza o resultado
   com `app.openapi()["paths"]` filtrado por `/admin`, que tem de trazer os mesmos pares de método e
   caminho. A primeira coluna é `` `MÉTODO /caminho` `` exatamente como o app registra.
2. Toda linha `falta` ou `feita` traz o `operation_id` (`snake_case`), a operação (`MÉTODO /caminho`,
   sob `/admin/api/v1`) e o papel, e as linhas que dividem uma operação (os destinos, o editor do PDF e
   o do formulário) dizem o mesmo e têm o mesmo status. Uma linha `feita` tem a operação no
   `docs/api/openapi.json`, com o método, o caminho, o papel e as marcas da linha; uma linha `falta` cuja
   operação já está no arquivo, ou `feita` cuja operação sumiu, falha. A linha `fora` não tem operação.
3. Toda linha `feita` cita `arquivo::teste`, e o teste existe, tem o nome começado por `test_` e fala da
   operação: o `operation_id`, ou o caminho dela (cada trecho fixo, como `/jobs` e `/pdf` em
   `/jobs/{public_id}/pdf`), aparece no código do teste ou no das funções e constantes do mesmo arquivo
   que ele usa (um auxiliar `create(...)`, uma constante `KEYS = f"{API}/api-keys"`). Cite o teste que
   exercita a operação, e não um qualquer do arquivo. **Quando o caminho da operação é prefixo do de
   outra** (`/jobs` e `/jobs/{public_id}`; ou o mesmo caminho com outro método), o caminho não diz qual
   das duas o teste exerce: o `operation_id` tem de estar no código do teste (um texto, um nome, uma
   constante; os testes usam `url_of("get_job", public_id=...)`, de `tests_app/admin_api_support.py`),
   e não vale só no docstring ou num comentário.
4. Toda linha `fora` tem uma letra que está em "O que não entra na API", e o conjunto das linhas `fora`
   é travado numa constante do teste (`OUT_OF_THE_API`): uma linha `fora` nova exige editá-la no mesmo
   PR, onde a revisão a vê.
5. Toda operação do `openapi.json` está em alguma linha, aqui ou nas operações novas.
6. Toda operação dos quatro editores (`FormEdit`, `PlanEdit`, `DataEdit` e `ScoringEdit`, que declaram
   a lista das suas em `OPERATIONS`) está listada nas tabelas acima, e as listadas existem no código. O
   nome é o que vem antes do primeiro `:` da primeira coluna (`remove-step:i` é `remove-step`), o editor
   é o do título da seção (`` (`FormEdit`, 19) ``), e a contagem do título tem de bater com a tabela.
7. Nenhuma linha está `falta` (a cobertura fechada). O número de linhas `falta` foi uma catraca, que só
   baixava a cada fase de recurso (a constante `MISSING_ROWS`, no teste); a última fase (#112) a levou a
   zero, e o teste passou a exigi-lo. A rota nova do admin sem linha reprova (regra 1), e a que chega
   com uma linha `falta` também: a operação da API vem no mesmo PR da rota, ou a linha é `fora (letra)`.

São **sete regras**, as mesmas da [ADR 0015](../adr/0015-api-de-admin.md#o-teste-de-cobertura) e do
contrato da API.

## Conclusão versionada

O documento editado por `save_form_draft`/`patch_form_draft` inclui `completion` (modo, mensagem, rótulo do botão e URL HTTPS). Os campos do editor têm equivalência direta com esses caminhos no documento; não são novas operações de editor. Importar, duplicar, exportar e publicar preservam o bloco pela mesma versão. Mudar somente conclusão não invalida teste LLM. `render_form_draft_completion_preview` está disponível no catálogo MCP derivado do OpenAPI, sem efeitos externos.


## Capturas sem geração

A entrega 8-A inclui autoria, recibo, consulta e retenção. As notificações assíncronas às
integrações aplicáveis e ativas pertencem à entrega 8-B da mesma issue; #8 permanece aberta.
`generation=none` dispensa conteúdo de geração e exige conclusão local, botão ou redirect;
`generation` ausente na definição preserva PDF; em `create_form`, herda a finalidade da origem
quando informada. Copiar captura com `generation=pdf` prepara o conteúdo de geração pelo modelo
inicial no novo rascunho, mantendo perguntas e respostas de exemplo; a origem não muda.

O detalhe HTML distingue somente captura e não mostra plano, prompt ou templates inexistentes,
antes ou depois da publicação. A montagem do prompt confere a finalidade no mesmo rascunho usado
na montagem; uma edição concorrente passa a valer na próxima operação. Se o rascunho lido é captura,
a montagem retorna `409 state_conflict`. O teste local de captura em HTML/API/MCP usa
`drafts.test_capture` e recusa rascunho PDF com `409`; não persiste submissão nem usa provedor.
As regressões públicas estão em `tests_app/test_capture_review_fixes.py`.

| Rota atual | O que faz | Serviço hoje | Operação da API | `operation_id` | Papel | Marcas | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `GET /admin/captures` | Lista estado sem PII; dados exigem total e opt-in | `queries.captures.listing` | `GET /captures` | `list_captures` | leitura | lead | feita · tests_app/test_capture.py::test_capture_is_consultable_without_disclosing_lead_data |
| `GET /admin/captures/{submission_id}` | Identidade histórica; sem token de recibo | `queries.captures.detail` | `GET /captures/{submission_id}` | `get_capture` | leitura | lead | feita · tests_app/test_capture.py::test_capture_is_consultable_without_disclosing_lead_data |
| `POST /admin/forms/{slug}/draft/capture-test` | Testa respostas, data, pontuação e conclusão | `drafts.test_capture` | `POST /forms/{slug}/draft/capture-test` | `test_capture_form_draft` | leitura | — | feita · tests_app/test_capture.py::test_capture_local_test_and_inapplicable_generation_operations |
