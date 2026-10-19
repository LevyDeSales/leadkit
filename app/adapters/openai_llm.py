"""Plano gerado pela OpenAI (Responses API) com saída estruturada. Usado com `LLM_MODE=openai`.

- `responses.create` direto no SDK, com `text.format` json_schema estrito (nunca
  `response_format`, que a Responses API não aceita). O schema é a versão enxuta do modelo de plano
  do formulário (`strict_schema`) e a resposta é validada depois com o modelo Pydantic completo
  e o `check_plan` do formulário (`spec.validate_plan`).
- Instruções = `spec.instructions`; entrada = `spec.render_prompt(answers)`, então só as respostas
  citadas no template do prompt vão para o provedor. `store=False`: nada fica guardado lá.
- Timeout próprio (`OPENAI_TIMEOUT_SECONDS`) e `max_retries=0`: quem repete é o Hatchet.
- Plano inválido: uma rodada de reparo com os erros de validação; depois, `plan_invalid`
  retentável. Recusa e filtro de conteúdo são definitivos; resposta incompleta é retentável.
- HTTP 408/409/429/5xx, timeout e falha de conexão: `llm_unavailable` retentável; outros 4xx:
  `llm_rejected` definitivo.
- Falha retentável no `OPENAI_MODEL` tenta uma vez o `OPENAI_FALLBACK_MODEL`, se houver.

As mensagens de erro vão para o Hatchet: só levam status, códigos e ids, nunca texto do provedor
nem valores da resposta (os dois podem ecoar dados do lead). Pelo mesmo motivo, o `usage` do plano
guarda os motivos de cada reparo só como `caminho:tipo` dos erros de validação.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from app.adapters.llm import LlmResult
from app.errors import FailureCode, RetryableError, TerminalError
from app.forms import FormSpec
from app.plan_timeout import MAX_REPAIRS
from app.settings import Settings

if TYPE_CHECKING:
    import httpx2
    from openai.types.responses import Response, ResponseInputParam, ResponseTextConfigParam

# Além de 5xx, os status que o próprio SDK repetiria (timeout, conflito de lock, rate limit).
RETRYABLE_STATUS = frozenset({408, 409, 429})
# Códigos que chegam com status retentável mas não melhoram com espera (cota da conta esgotada).
TERMINAL_CODES = frozenset({"insufficient_quota"})
# Palavras-chave que o modo estrito aceita em qualquer provedor. Faixas e formatos (minLength,
# maxItems, pattern...) saem do schema e continuam valendo na validação Pydantic da resposta. Do
# mesmo jeito, o `enum` ou `const` com um valor entre aspas duplas (`Checklist "Antes de Passar
# Preço"`) sai do nó, porque o modo estrito recusa `"` em literal de texto; a lista passa à
# descrição (`quoted_literal_to_description`).
STRICT_KEYWORDS = frozenset(
    {"type", "properties", "items", "anyOf", "enum", "const", "description", "$ref", "$defs"}
)


class OpenAIPlanGenerator:
    """Sem estado por chamada: uma instância atende todas as tasks do worker em paralelo."""

    def __init__(self, client: OpenAI, *, model: str, fallback_model: str | None = None) -> None:
        self.client = client
        self.model = model
        self.fallback_model = fallback_model

    @classmethod
    def from_settings(
        cls, settings: Settings, *, http_client: httpx2.Client | None = None
    ) -> OpenAIPlanGenerator:
        """`http_client` só para testes (transporte simulado)."""
        if settings.openai_api_key is None or not settings.openai_model:
            raise ValueError("LLM_MODE=openai exige OPENAI_API_KEY e OPENAI_MODEL")
        # Conectar (TCP e TLS) tem limite curto, que vale para cada endereço IP tentado. Só com o
        # timeout da chamada, uma rota IPv6 quebrada segurava a chamada em cada endereço IPv6 até
        # o limite do sistema antes de o cliente tentar o IPv4.
        timeout = openai.Timeout(
            settings.openai_timeout_seconds, connect=settings.openai_connect_timeout_seconds
        )
        client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )
        return cls(
            client, model=settings.openai_model, fallback_model=settings.openai_fallback_model
        )

    def generate(self, spec: FormSpec, answers: BaseModel) -> LlmResult:
        prompt = spec.render_prompt(answers)
        tokens: Counter[str] = Counter()  # soma de todas as chamadas, inclusive as que falharam
        fallback = False
        try:
            plan, response, repairs, reasons = self._generate_with(
                self.model, spec, answers, prompt, tokens
            )
        except RetryableError as primary:
            if self.fallback_model is None:
                raise
            try:
                plan, response, repairs, reasons = self._generate_with(
                    self.fallback_model, spec, answers, prompt, tokens
                )
            except (RetryableError, TerminalError) as failure:
                # O fallback é uma segunda chance: se ele falhar, o erro continua retentável.
                message = f"fallback também falhou: {failure}"
                raise RetryableError(primary.code, message) from primary
            fallback = True
        return LlmResult(
            document=plan.model_dump(mode="json"),
            model=response.model,
            usage={
                "model": response.model,
                "response_id": response.id,
                "repairs": repairs,
                **({"repair_reasons": reasons[:20]} if reasons else {}),
                "fallback": fallback,
                **tokens,
            },
        )

    def _generate_with(
        self, model: str, spec: FormSpec, answers: BaseModel, prompt: str, tokens: Counter[str]
    ) -> tuple[BaseModel, Response, int, list[str]]:
        """Pede o plano a um modelo, com uma rodada de reparo. Devolve (plano, resposta, reparos,
        motivos dos reparos)."""
        text: ResponseTextConfigParam = {
            "format": {
                "type": "json_schema",
                "name": f"plano_{spec.slug}",
                "strict": True,
                "schema": strict_schema(spec.plan_model),
            }
        }
        messages: ResponseInputParam = [{"role": "user", "content": prompt}]
        reasons: list[str] = []
        for repairs in range(MAX_REPAIRS + 1):
            response = self._create(model, spec.instructions, messages, text)
            usage = response.usage.model_dump() if response.usage else {}
            tokens.update({key: value for key, value in usage.items() if isinstance(value, int)})
            output = plan_text(response)
            try:
                return spec.validate_plan(output, answers), response, repairs, reasons
            except ValidationError as exc:
                if repairs == MAX_REPAIRS:
                    # `from None`: o ValidationError traz os valores recebidos e iria para o Hatchet.
                    raise RetryableError(
                        FailureCode.PLAN_INVALID, invalid_plan_message(exc)
                    ) from None
                reasons += repair_reasons(exc, spec.plan_model)
                messages += [
                    {"role": "assistant", "content": output},
                    {"role": "user", "content": repair_prompt(exc)},
                ]
        raise AssertionError("inalcançável")

    def _create(
        self,
        model: str,
        instructions: str,
        messages: ResponseInputParam,
        text: ResponseTextConfigParam,
    ) -> Response:
        try:
            return self.client.responses.create(
                model=model, instructions=instructions, input=messages, text=text, store=False
            )
        except openai.APIStatusError as exc:
            # `from None`: a mensagem do SDK traz o corpo da resposta, que pode ecoar a entrada.
            raise status_error(exc) from None
        except openai.APIConnectionError as exc:  # inclui APITimeoutError
            raise RetryableError(FailureCode.LLM_UNAVAILABLE, type(exc).__name__) from exc


def strict_schema(plan_model: type[BaseModel]) -> dict[str, Any]:
    """Schema enxuto do modelo de plano para structured outputs em modo estrito: todo objeto com
    todas as chaves obrigatórias e `additionalProperties: false`, só com `STRICT_KEYWORDS`.
    Descrições (inclusive docstrings dos modelos) vão junto e servem de instrução ao modelo.

    O modo estrito recusa aspas duplas em literal de texto (HTTP 400 `invalid_json_schema`). Um
    `enum` ou `const` com `"` em algum valor sai do nó e os valores permitidos entram na descrição
    (`Valores permitidos: a; b; c.`): o modelo ainda vê a lista, e a validação Pydantic da resposta
    continua recusando o que estiver fora dela (a rodada de reparo corrige)."""
    root = plan_model.model_json_schema()
    definitions = root.get("$defs", {})

    def lean(node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref")
        if ref is not None and len(node) > 1:  # `$ref` não aceita vizinhas: expande a definição
            siblings = {key: value for key, value in node.items() if key != "$ref"}
            node = {**definitions[ref.rsplit("/", 1)[-1]], **siblings}
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key not in STRICT_KEYWORDS:
                continue
            if key in ("properties", "$defs"):
                out[key] = {name: lean(subschema) for name, subschema in value.items()}
            elif key == "items":
                out[key] = lean(value)
            elif key == "anyOf":
                out[key] = [lean(subschema) for subschema in value]
            else:
                out[key] = value
        quoted_literal_to_description(out)
        if out.get("type") == "object":
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}))
        return out

    return lean(root)


def quoted_literal_to_description(node: dict[str, Any]) -> None:
    """Se o `enum` ou o `const` do nó tem um valor de texto com aspas duplas, tira os dois do nó
    e acrescenta os valores permitidos à descrição. Muda o nó no lugar."""
    allowed = [*node.get("enum", []), *([node["const"]] if "const" in node else [])]
    if not any(isinstance(value, str) and '"' in value for value in allowed):
        return
    node.pop("enum", None)
    node.pop("const", None)
    description = node.get("description", "").rstrip()
    if description and not description.endswith((".", "!", "?")):
        description += "."
    listed = f"Valores permitidos: {'; '.join(str(value) for value in allowed)}."
    node["description"] = f"{description} {listed}".strip()


def plan_text(response: Response) -> str:
    """Texto do plano, ou o erro certo se o modelo recusou ou não terminou a resposta. Nesses
    casos o reparo não ajuda, e o texto da recusa (que pode citar o lead) não vai para o erro."""
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        if reason == "content_filter":
            raise TerminalError(FailureCode.LLM_REJECTED, "resposta barrada pelo filtro")
        raise RetryableError(FailureCode.PLAN_INVALID, f"resposta incompleta ({reason})")
    if response.status not in (None, "completed"):
        code = response.error.code if response.error else None
        raise RetryableError(FailureCode.LLM_UNAVAILABLE, f"resposta {response.status} ({code})")
    for item in response.output:
        if item.type == "message" and any(part.type == "refusal" for part in item.content):
            raise TerminalError(FailureCode.LLM_REJECTED, "o modelo recusou gerar o plano")
    return response.output_text


def status_error(exc: openai.APIStatusError) -> RetryableError | TerminalError:
    """Status HTTP do provedor -> erro do pipeline, com status, código e id da requisição."""
    detail = f"HTTP {exc.status_code}"
    if exc.code:
        detail += f" {exc.code}"
    if exc.request_id:
        detail += f" ({exc.request_id})"
    if exc.code in TERMINAL_CODES:
        return TerminalError(FailureCode.LLM_REJECTED, detail)
    if exc.status_code >= 500 or exc.status_code in RETRYABLE_STATUS:
        return RetryableError(FailureCode.LLM_UNAVAILABLE, detail)
    return TerminalError(FailureCode.LLM_REJECTED, detail)


def repair_prompt(error: ValidationError) -> str:
    """Erros de validação para o modelo corrigir: caminho e regra, nunca o valor recebido."""
    lines = [
        f"- {'.'.join(str(part) for part in item['loc']) or 'plano'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False)[:20]
    ]
    return (
        "A resposta anterior não passou na validação do plano:\n"
        + "\n".join(lines)
        + "\nCorrija esses pontos e devolva o plano completo, no mesmo formato."
    )


def repair_reasons(error: ValidationError, plan_model: type[BaseModel]) -> list[str]:
    """Motivos do reparo para o `usage` do plano: `caminho:tipo` de cada erro, como
    `fases.0.nome:value_error`, nunca o valor nem a mensagem. No caminho, o que não é nome de
    campo do plano (chave inventada pelo modelo, que pode ecoar o lead) vira `*`."""
    schema = plan_model.model_json_schema()
    names = {
        name
        for node in [schema, *schema.get("$defs", {}).values()]
        for name in node.get("properties", {})
    }

    def path(loc: tuple[int | str, ...]) -> str:
        parts = (str(part) if isinstance(part, int) or part in names else "*" for part in loc)
        return ".".join(parts) or "plano"

    return [
        f"{path(item['loc'])}:{item['type']}"
        for item in error.errors(include_input=False, include_url=False)
    ]


def invalid_plan_message(error: ValidationError) -> str:
    """Resumo seguro para o Hatchet: quantos erros e de que tipos (identificadores do pydantic)."""
    kinds = ", ".join(sorted({item["type"] for item in error.errors(include_input=False)}))
    return f"plano inválido depois do reparo: {error.error_count()} erro(s) ({kinds})"
