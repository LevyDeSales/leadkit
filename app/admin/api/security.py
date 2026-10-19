"""A autenticação da API de admin: `Authorization: Bearer <chave>` e o papel de cada operação.

Cada operação declara o papel mínimo numa dependência, `requires("leitura")` ou `requires("total")`
(`OperationRouter.operation` a põe; ver `app.admin.api.operations`), e a mesma declaração gera o
`x-required-role` do OpenAPI. **Negado por padrão:** uma rota da API sem `requires` derruba a montagem
(`OperationRoute`, e `refuse_unguarded_routes` para a que nem é de operação).

O que `requires` faz, nesta ordem:

1. **Bloqueio por IP.** O `LoginThrottle` do login (a 5ª falha seguida bloqueia 1 minuto, dobrando
   até 15). Bloqueado, responde `429 rate_limited` com `Retry-After`, mesmo para uma chave certa:
   senão o bloqueio diria qual palpite acertou. Só conta falha quem mandou um `Authorization` que não
   vale (malformado, desconhecido ou de segredo errado); a chave vencida ou revogada acertou o
   segredo, e a ausência do cabeçalho não é palpite. Uma chave que vale zera a contagem do IP.
2. **A chave.** O cabeçalho bruto vai para `keys.authenticate`, o único lugar que o lê. Ela corre
   numa transação própria e curta, **confirmada antes de a operação rodar**: o `UPDATE` do
   `last_used_at` segura o lock da linha da chave até o commit, e uma operação demorada (renderizar um
   PDF, falar com o Baserow) deixaria a chave travada se dividisse a transação. `invalid_credentials`,
   `key_expired` e `key_revoked` são `401` com `WWW-Authenticate: Bearer`.
3. **O balde da chave** (`app.admin.api.limits`): `429 rate_limited` com `Retry-After`.
4. **O papel.** `keys.allows(papel da chave, papel exigido)`: a ordem dos papéis mora só no serviço.
   Chave `leitura` numa operação `total` é `403 forbidden_role`.

**Antes do corpo grande.** O FastAPI lê o corpo de um formulário (`multipart`, o envio de uma imagem)
antes de rodar as dependências, e o arquivo iria inteiro para a memória e o disco de quem nem tem
chave. Por isso a rota de um corpo de formulário (`OperationRoute.get_route_handler`) roda estes passos
**antes** de lê-lo, e a dependência `requires` reconhece que a chamada já passou (`authenticate` é
idempotente na mesma chamada).

Só o cabeçalho vale. Cookie (a sessão do admin) e query string (`?api_key=`) são ignorados; por isso
nenhuma operação que escreve precisa de token CSRF. O `HTTPBearer(auto_error=False)` está na
assinatura só para o OpenAPI descrever o esquema de segurança (e a referência mostrar o campo da
chave); ele não lê nem recusa nada.

Um chamador que já passou por `requires` encontra a chave em `CurrentKey`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.admin.api import keys
from app.admin.api.errors import ApiError
from app.admin.auth import instant
from app.deps import get_deps
from app.models import API_KEY_ROLES

logger = logging.getLogger(__name__)

BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="ApiKeyAuth",
    bearerFormat="gp_<id>_<segredo>",
    description=(
        "Uma chave de API do admin, no cabeçalho `Authorization: Bearer <chave>`. As chaves se criam "
        "em `/admin/api-keys` (ou por `create_api_key`); a chave inteira aparece uma vez só, na "
        "criação. Não vale na URL nem em cookie."
    ),
)
BLOCKED = "Muitas chaves erradas deste endereço. Tente de novo em instantes."
OVER_THE_LIMIT = "Esta chave passou do limite de chamadas por minuto. Tente de novo em instantes."


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "desconhecido"


def _authenticate(authorization: str | None, now: datetime) -> keys.KeyInfo:
    """A transação própria e curta da autenticação: confirmada ao sair do `with`, antes de a
    operação abrir a dela."""
    with get_deps().sessions.begin() as session:
        return keys.authenticate(session, authorization, now=now)


class RoleGate:
    """A dependência de uma operação: autentica a chave, aplica o limite de uso e confere o papel.
    `role` é o papel mínimo e `cost`, as fichas que a chamada gasta do balde."""

    def __init__(self, role: str, cost: int = 1) -> None:
        if role not in API_KEY_ROLES:
            raise ValueError(f"papel desconhecido: {role!r}")
        self.role = role
        self.cost = cost

    async def __call__(
        self,
        request: Request,
        _credentials: Annotated[HTTPAuthorizationCredentials | None, Security(BEARER)],
    ) -> keys.KeyInfo:
        return await self.authenticate(request)

    async def authenticate(self, request: Request) -> keys.KeyInfo:
        """Os quatro passos acima, uma vez por chamada: quem os repete na mesma chamada (a rota de um
        corpo de formulário os faz antes de ler o corpo, e a dependência os faz de novo) recebe a chave
        que passou, sem gastar outra ficha do balde nem contar outra tentativa."""
        if getattr(request.state, "gate", None) is self:
            return current_key(request)
        state = request.app.state
        ip = client_ip(request)
        wait = state.throttle.retry_after(ip)
        if wait:
            raise ApiError("rate_limited", BLOCKED, retry_after=wait)
        authorization = request.headers.get("authorization")
        try:
            key = await run_in_threadpool(_authenticate, authorization, instant(state.clock))
        except keys.AuthFailed as failed:
            logger.warning(
                "API de admin: chave recusada motivo=%s ip=%s key_id=%s",
                failed.code,
                ip,
                failed.key_id or "-",
            )
            if failed.code == "invalid_credentials" and authorization is not None:
                wait = state.throttle.failure(ip)
                if wait:
                    raise ApiError("rate_limited", BLOCKED, retry_after=wait) from None
            raise ApiError(
                failed.code, str(failed), headers={"WWW-Authenticate": "Bearer"}
            ) from None
        state.throttle.success(ip)
        wait = state.limiter.take(key.key_id, self.cost)
        if wait:
            raise ApiError("rate_limited", OVER_THE_LIMIT, retry_after=wait)
        ensure_role(key, self.role)
        request.state.api_key = key
        request.state.gate = self
        return key


def ensure_role(key: keys.KeyInfo, role: str, *, what: str = "Esta operação") -> None:
    """`403 forbidden_role` se o papel da chave não chega a `role`: o mesmo erro da operação que pede
    o papel inteiro (`requires`), para a operação que **pede mais papel só para uma parte** dela. É o
    caso de `get_job`: `leitura` lê o estado do pedido, mas o `lead_data` é só de `total`. `what`
    completa a frase da recusa, para dizer qual parte pediu o papel. Quem a usa declara o `403` da
    operação (`errors={403: ...}`), porque o papel mínimo dela, `leitura`, não o traz sozinho."""
    if not keys.allows(key.role, role):
        raise ApiError(
            "forbidden_role",
            f"{what} pede uma chave com papel «{role}»; a sua tem papel «{key.role}».",
        )


def requires(role: str, *, cost: int = 1) -> RoleGate:
    """O papel mínimo da operação (`leitura` ou `total`). `cost` é o preço da chamada em fichas do
    balde (`app.admin.api.limits.HEAVY_COST` para a pesada)."""
    return RoleGate(role, cost)


def current_key(request: Request) -> keys.KeyInfo:
    """A chave que fez a chamada, já autenticada por `requires`. Sem ele, é erro de quem montou a
    rota (a montagem da API a recusa)."""
    key: keys.KeyInfo | None = getattr(request.state, "api_key", None)
    if key is None:
        raise RuntimeError("a rota usa CurrentKey sem passar por requires()")
    return key


CurrentKey = Annotated[keys.KeyInfo, Depends(current_key)]
