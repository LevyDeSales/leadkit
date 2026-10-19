# Decisões de arquitetura (ADRs)

Cada arquivo registra uma decisão difícil de desfazer, que surpreenderia quem lê o código sem o
contexto, e que veio de uma escolha real entre alternativas. O registro diz o contexto, a decisão
e as consequências; o detalhe fica nos relatórios e no código citados em cada um.

| ADR | Decisão | Data |
| --- | --- | --- |
| [0001](0001-reescrita-python-fastapi-hatchet.md) | Reescrever o runtime em Python com FastAPI e Hatchet | 2026-10-07 |
| [0002](0002-hatchet-cloud-primeiro.md) | Rodar o pipeline no Hatchet Cloud primeiro | 2026-10-07 |
| [0003](0003-jobs-como-outbox-e-reconciliador.md) | A tabela `jobs` faz o papel de outbox, com um cron reconciliador | 2026-10-07 |
| [0004](0004-formularios-como-configuracao.md) | Formulários e o contexto deles são configuração, não código do motor (substituída pela 0013) | 2026-10-07 |
| [0005](0005-openai-saida-estruturada.md) | Plano pela OpenAI Responses API com saída estruturada e uma rodada de reparo | 2026-10-07 |
| [0006](0006-pdf-weasyprint-jinja2.md) | PDF com WeasyPrint e Jinja2 dentro do worker | 2026-10-07 |
| [0007](0007-pdfs-no-s3.md) | PDFs num bucket S3, não no Postgres | 2026-10-07 |
| [0008](0008-link-da-lead-assinado-com-hmac.md) | Link da lead assinado com HMAC, sem tabela de tokens, trocado por cookie | 2026-10-07 |
| [0009](0009-webhook-como-unico-destino.md) | O webhook é o único destino de entrega | 2026-10-07 |
| [0010](0010-paginas-jinja2-e-htmx.md) | Páginas renderizadas no servidor com Jinja2 e HTMX, sem SPA | 2026-10-07 |
| [0011](0011-retencao-lgpd-apaga.md) | A retenção LGPD apaga os dados, em vez de anonimizar | 2026-10-08 |
| [0012](0012-deploy-compose-ou-swarm-com-traefik.md) | Dois jeitos de subir: compose com Caddy ou a stack de `deploy/swarm/` atrás de um Traefik | 2026-10-08 |
| [0013](0013-formularios-no-banco-com-versoes.md) | Formulários no banco, em versões que não mudam depois de publicadas, criados no admin | 2026-10-09 |
| [0014](0014-integracao-de-planilha-por-formulario.md) | Uma integração de planilha (Baserow) por formulário, ao lado do webhook | 2026-10-15 |
| [0015](0015-api-de-admin.md) | API de admin com cobertura total do painel, chaves com papel, referência Scalar e um MCP derivado do OpenAPI | 2026-10-15 |

Todas estão com status **aceita**, menos a 0004, **substituída** pela 0013.
