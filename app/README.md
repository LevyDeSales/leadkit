# Referência técnica — Leadkit

O motor recebe um formulário em `/p/{slug}` ou `POST /v1/forms/{slug}/submissions`, valida as
respostas contra a versão publicada e cria um pedido idempotente. Tasks Hatchet geram o plano,
validam o documento, renderizam o PDF e entregam `plano.concluido` a um webhook assinado.
O reconciliador recupera trabalhos pendentes e a retenção remove dados vencidos.

## Configuração

Copie [app/.env.example](.env.example) para `.env` e use valores próprios. As opções e validações
estão em [settings.py](settings.py): `DATABASE_URL`, `PUBLIC_BASE_URL`, `TOKEN_SECRETS`, Hatchet,
retenção, LLM, PDF, storage e admin. `DATABASE_URL` precisa também ser exportada no shell para
Alembic; ele não carrega `.env`.

`LLM_MODE=fake` usa o plano de exemplo da versão; `PDF_MODE=weasyprint` gera PDF real;
`STORAGE_MODE=local` grava em `LOCAL_STORAGE_DIR`. O worker ainda precisa de Hatchet. Para uso com
provedores, configure OpenAI e S3 com credenciais da sua instalação. Não coloque contatos da lead
nos inputs/outputs ou logs das tasks.

## Administradora

Defina juntas `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` (Argon2) e `ADMIN_SESSION_SECRET` (32 ou mais
caracteres aleatórios). Gere o hash sem colocar a senha no histórico do shell com
`python -c 'from argon2 import PasswordHasher; from getpass import getpass; print(PasswordHasher().hash(getpass()))'` e guarde os segredos no
ambiente. O admin fica em `/admin`; suas páginas permitem importar o demo, editar o rascunho,
executar teste e publicar. Uma publicação fixa a definição e as imagens usadas por ela.

`WEBHOOK_ENCRYPTION_KEY` deve ser uma chave Fernet válida, gerada e guardada pela operadora:
`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.
Mantenha a chave em backups seguros: trocá-la sem o procedimento de rotação impede ler conexões.

## Formulários e templates

O exemplo completo está em [examples/demo.json](../examples/demo.json); o starter do admin fica
em [forms/starter](forms/starter/definition.json). O compilador documenta o formato 1 em
[forms/definition](forms/definition/__init__.py). Ele valida perguntas, condições, mapeamento de
identidade, consentimentos, dados, pontuação, calendário e modelo do plano.

O prompt recebe `answers`, `labels`, `scores`, `today`, `data` e `phases`. PDF e prévia recebem
`plan`, `lead.name`, `form.title`, `form.slug`, `scores` liberados, `generated_date` e macros `brand`.
Contato não deve ser interpolado no prompt. Imagens do formulário usam `asset:nome` e ficam fixadas
por conteúdo na versão. O PDF aceita recursos locais confinados, `data:` e imagens do formulário;
URLs de rede são recusadas. Veja [marca privada](../docs/branding.md).

### Conclusão após salvar respostas

O bloco opcional `completion` pertence à versão publicada. Sem ele, a participante acompanha o
resultado do PDF como antes (`mode: result`). Os outros modos são `confirmation` (recibo local),
`button` (recibo com botão) e `redirect` (recibo com encaminhamento automático e botão de continuidade).
`message` é texto puro de 1 a 2000 caracteres, com padrão “Recebemos suas respostas. Obrigada por
participar.”; `button_label` tem de 1 a 300 caracteres e padrão “Continuar”. HTML é escapado e texto
não é interpretado como template. O botão de envio do formulário mantém o texto legado.

`url` é obrigatória em `button`/`redirect` e não se aplica aos modos locais. A configuração
administrativa autoriza a URL HTTPS exata, sem usuário/senha ou override público; o produto não
acrescenta respostas, dados pessoais ou credenciais a ela. Não há lista global adicional de domínios.
Sem JavaScript, a participante usa o botão do recibo. O botão continua disponível ao voltar à página.

Editor, API e MCP editam o mesmo documento. A prévia “Conferir conclusão salva” usa o rascunho,
mostra o destino como texto e desativa a navegação; também está disponível na operação
`render_form_draft_completion_preview`. Mudar somente conclusão não invalida o teste do LLM.
Importação, cópia e exportação conservam a configuração pela versão.

A confirmação só acontece após commit, e reenvios conservam a versão original, mesmo após nova
publicação. O disparo de geração inicia sem prender a resposta; falha ou interrupção conserva o job
persistido para o reconciliador. A API pública mantém os campos do pedido e acrescenta `completion`,
incluindo `receipt_url` local. O token desse recibo é trocado por cookie como no resultado existente.
Não envie esse token ao destino externo. A conclusão não bloqueia nem libera PDF: a geração e os
acessos atuais continuam funcionando. Esta opção ainda opera apenas com geração de PDF.

## API e MCP

A especificação está em [docs/api/openapi.json](../docs/api/openapi.json); a referência interativa
fica em `/admin/api`. Chaves de API têm papéis próprios e não substituem a sessão do admin.
A página `/admin/mcp` fornece a configuração do cliente e as instruções para criar uma chave.
O servidor pode ser executado com `python -m app.admin_mcp`; preserve o nome das ferramentas e
os contratos HTTP ao integrar clientes existentes.

O venv de uma instalação editável do MCP aponta para o clone. Mantenha a pasta no mesmo caminho e
na versão escolhida; mover ou apagar a pasta quebra a instalação. O campo de Python aceita um venv
em outro disco. As datas de validade da chave na página usam o horário de Brasília.

## Webhook

O corpo JSON é assinado com HMAC-SHA256; os nomes dos headers `X-MCC-Event`, `X-MCC-Timestamp` e
`X-MCC-Signature` são contratos de compatibilidade. Confira timestamp e assinatura antes de
processar o evento; repetições devem ser idempotentes. Não registre o corpo em logs públicos.

## Deploy e retenção

Os exemplos [Compose](../deploy/compose.yaml) e [Swarm](../deploy/swarm/README.md) precisam de
configuração própria. Migre antes de iniciar o serviço e use uma versão fixada. Teste backup e
restauração em ambiente isolado antes de atualizar. Não use a baseline de banco vazio sobre uma
instalação existente sem um plano de compatibilidade aprovado.

`LINK_TTL_DAYS`, `RETENTION_PDF_DAYS` e `RETENTION_DAYS` controlam validade e limpeza; formulários
podem definir prazos próprios compatíveis. A exclusão lógica do link e a remoção física do PDF
são etapas distintas. Quem opera o sistema é responsável por consentimento, retenção e fornecedores.

O [roteiro de migração](../docs/migrations.md) detalha o guard, os ensaios e a preservação de
fonte, código, dados, storage, marca e chaves na futura adoção do núcleo.
