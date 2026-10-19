"""A referência da API de admin: a página do Scalar e o `openapi.json` que ela lê (ADR 0015, "A
referência Scalar").

    GET /admin/api/v1/reference      a página
    GET /admin/api/v1/openapi.json   o arquivo, o mesmo `docs/api/openapi.json`

As duas rotas ficam **fora do próprio esquema** (`include_in_schema=False`) e são as únicas da API
sem papel: moram num `OpenRouter`, a exceção declarada à regra de que toda rota pede chave, e quem
responde pela autenticação delas é este módulo.

**Quem entra.**

- Em `development` e `test`, qualquer um (a documentação de quem desenvolve).
- Em produção, uma sessão do admin (nestas duas rotas, que só leem) ou uma chave de qualquer papel
  no cabeçalho `Authorization: Bearer`. Sem nenhuma, a página redireciona ao login (`303`, com o
  `next` de volta à referência) e o JSON responde `401`, como as operações. Mandou o cabeçalho e ele
  não vale: `401` em JSON nas duas, com o motivo de sempre, e a falha conta no bloqueio por IP da
  chave errada, porque a conferência é a mesma das operações (`requires("leitura")`, o papel mais
  baixo, que qualquer chave tem).
- **As operações da API não ficam abertas em ambiente nenhum**, nem com sessão: o Scalar pede a chave
  no campo de autenticação, e é com ela que "Test Request" chama a operação. Só estas duas rotas leem
  a sessão.

A sessão do painel mora no `AdminAuth` do app principal, que a sub-aplicação não enxerga (`request.
app` é ela, não o app pai): `create_admin_api` recebe o `AdminAuth` e o ambiente na montagem e os
guarda em `app.state`. Sem eles, a sub-aplicação vale como produção sem sessão: só uma chave entra
(fechada por padrão, para quem monta uma sem dizer o ambiente). Ler a referência não renova o cookie
da sessão: quem o renova são as páginas do painel.

**Sem terceiros.** O bundle do Scalar (`app/web/static/vendor/scalar-<versão>.js`, com a licença e o
sha256 no `NOTICE.txt`) entra por `static_url`, e a página não tem script nem estilo embutido: o
`<script id="api-reference">` só carrega atributos (`data-url` e `data-configuration`) que o Scalar
lê, e a configuração desliga o proxy, a telemetria e as fontes de fora. A política de conteúdo é
própria da página (as outras do admin ficam com a de `app.admin.routes.CSP`): `script-src 'self'`,
`connect-src 'self'` e nenhuma origem externa. O que o bundle pede além disso, medido num navegador
com o Scalar 1.74.0: estilo embutido. Ele injeta um `<style>` de 276 KB, que aceitaria o nonce da
página (`<meta property="csp-nonce">`), e mais três que não o recebem (dois das notificações, o
`vue-sonner` e o ajuste do Scalar sobre ele, e o do tema), e põe `style` em elementos. Com um nonce
na política o navegador ignora o `'unsafe-inline'` e barra esses três; por isso `style-src` ganha
`'unsafe-inline'`, sem nonce, e só ele: o script segue só `'self'`, sem
`'unsafe-inline'` nem `'unsafe-eval'`. O que sobra é uma violação de `eval` que o próprio bundle
provoca de propósito (o zod testa se `new Function` roda e, não rodando, valida sem ele): aparece no
console e não muda nada. Atualizar o Scalar é trocar o arquivo, o `SCALAR_VERSION`, o sha256 do
`NOTICE.txt` e a lista dos terceiros (`app.web.scalar_licenses`), e medir de novo (`CONTRIBUTING.md`, "Referência da API: o bundle do Scalar").

**O JSON.** É o `docs/api/openapi.json`, mas gerado na hora a partir da sub-aplicação, e não lido do
disco: a imagem do app (`Dockerfile`) leva só `app/` e `deploy/`, sem `docs/`. O mesmo texto (`app.
admin.api.document.render`) é o do arquivo, e o teste de `openapi.json` já cobra que o arquivo
versionado é o que a API gera.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from app.admin.api.document import render
from app.admin.api.errors import PREFIX
from app.admin.api.operations import OpenRouter
from app.admin.api.security import requires
from app.admin.auth import COOKIE_NAME
from app.admin.routes import SECURITY_HEADERS
from app.web.templating import templates

# Os ambientes em que a referência abre sem login nem chave. O resto (hoje, `production`) pede.
OPEN_ENVIRONMENTS = frozenset({"development", "test"})
# A versão do Scalar vendorizado: o nome do arquivo em `static/vendor/`. Atualizar é trocar o arquivo,
# esta constante e a linha do `NOTICE.txt` (com o sha256).
SCALAR_VERSION = "1.74.0"
BUNDLE = f"vendor/scalar-{SCALAR_VERSION}.js"
REFERENCE_PATH = f"{PREFIX}/reference"
OPENAPI_PATH = f"{PREFIX}/openapi.json"
LOGIN_URL = "/admin/login?" + urlencode({"next": REFERENCE_PATH})

# O que o Scalar lê de `data-configuration`. Cada opção desliga algo que sairia da origem ou que a
# referência não precisa (os nomes saem da versão vendorizada):
CONFIGURATION: dict[str, Any] = {
    "url": OPENAPI_PATH,
    "proxyUrl": "",  # sem o proxy do Scalar (proxy.scalar.com): "Test Request" vai direto à API
    "telemetry": False,
    "withDefaultFonts": False,  # sem Google Fonts; a página usa as fontes do sistema
    "hideClientButton": True,  # o botão que abre o client.scalar.com
    "agent": {"disabled": True},  # o assistente do Scalar (um serviço de fora)
    "mcp": {"disabled": True},
    "persistAuth": False,  # a chave digitada fica na memória da página, não no localStorage
    "showDeveloperTools": "never",  # a barra de ferramentas do Scalar, que leva a sites dele
    "hideTestRequestButton": False,  # testar uma operação (com a chave) segue possível
    "documentDownloadType": "json",
    "layout": "modern",
    # O campo da chave já aparece escolhido: o esquema `ApiKeyAuth` (Bearer) da API.
    "authentication": {"preferredSecurityScheme": "ApiKeyAuth"},
}

# A política de conteúdo da página (a das outras páginas do admin é `app.admin.routes.CSP`). Só o
# `style-src` é mais largo que a delas, pelo que o Scalar mede (ver o texto do módulo): o script
# segue só `'self'`.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
    "form-action 'none'; base-uri 'none'; object-src 'none'"
)

router = OpenRouter()
# O papel mais baixo: qualquer chave o tem. É a mesma conferência das operações (bloqueio por IP,
# balde de uso, chave vencida ou revogada), e a que responde o 401 de quem não mandou nada.
ANY_KEY = requires("leitura")


def _opened_by_environment_or_session(request: Request) -> bool:
    state = request.app.state
    if state.environment in OPEN_ENVIRONMENTS:
        return True
    auth = state.admin_auth
    return auth is not None and auth.read(request.cookies.get(COOKIE_NAME)) is not None


@router.get("/reference", include_in_schema=False)
async def reference(request: Request) -> Response:
    if not _opened_by_environment_or_session(request):
        if request.headers.get("authorization") is None:
            return RedirectResponse(LOGIN_URL, status_code=303)
        await ANY_KEY(request, None)  # mandou uma chave: vale ou é o 401 de sempre
    return templates.TemplateResponse(
        request,
        "admin/api_reference.html",
        {"configuration": CONFIGURATION, "bundle": BUNDLE, "url": OPENAPI_PATH},
        headers={**SECURITY_HEADERS, "Content-Security-Policy": CSP},
    )


@router.get("/openapi.json", include_in_schema=False)
async def openapi_document(request: Request) -> Response:
    if not _opened_by_environment_or_session(request):
        await ANY_KEY(request, None)  # sem chave, ou com uma que não vale: o 401
    state = request.app.state
    if getattr(state, "openapi_text", None) is None:
        state.openapi_text = render(request.app.openapi())
    return Response(state.openapi_text, media_type="application/json")
