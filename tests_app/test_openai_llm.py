"""Plano gerado pela OpenAI: o SDK `openai` de verdade sobre um transporte HTTP simulado, direto
no adaptador e pelo passo de plano do pipeline (Postgres de teste).

O SDK 3.x usa o `httpx2` como camada HTTP, então o transporte simulado é o `httpx2.MockTransport`.
Nenhuma requisição sai da máquina.
"""

from __future__ import annotations

import json
import os
import traceback
from collections.abc import Iterator
from dataclasses import replace
from typing import Any, Literal

import httpx2
import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, LEGACY_SLUGS, legacy_definition, legacy_spec
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from test_intake import submit

from app import jobs, pipeline
from app.adapters.openai_llm import OpenAIPlanGenerator, strict_schema
from app.deps import Deps, build_llm
from app.errors import FailureCode, RetryableError, TerminalError
from app.models import Job, Plan
from app.settings import Settings

DEMO = legacy_spec("demo")
ANSWERS = DEMO.answers_model.model_validate(DEMO_ANSWERS)

Reply = dict[str, Any] | httpx2.Response | Exception


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acao: str = Field(min_length=5, max_length=200)
    prazo: Literal["semana", "mes"]


class NestedPlan(BaseModel):
    """Plano com submodelo descrito, lista de submodelos, faixas e campo opcional."""

    model_config = ConfigDict(extra="forbid")

    titulo: str = Field(min_length=3, max_length=120, description="título curto")
    primeiro_passo: Step = Field(description="o que fazer primeiro")
    passos: list[Step] = Field(min_length=2, max_length=4)
    observacao: str | None = None


NESTED_PLAN = {
    "titulo": "Plano em passos",
    "primeiro_passo": {"acao": "Listar os clientes ativos", "prazo": "semana"},
    "passos": [
        {"acao": "Padronizar a proposta comercial", "prazo": "mes"},
        {"acao": "Revisar os números toda sexta", "prazo": "semana"},
    ],
    "observacao": None,
}


QUOTED = 'Recurso ecossistema 21 "Exemplo"'


class QuotedChoices(BaseModel):
    """Plano com listas fechadas: duas com aspas duplas num valor, uma única e uma sem aspas."""

    model_config = ConfigDict(extra="forbid")

    entregavel: Literal['Recurso ecossistema 21 "Exemplo"', "Guia Simples", "Plano A"] = Field(
        description="o entregável"
    )
    entregaveis: list[Literal['Recurso ecossistema 21 "Exemplo"', "Guia Simples"]]
    unico: Literal['Só o "único"']
    prazo: Literal["semana", "mes"]


# Palavras-chave que o modo estrito não aceita (ou aceita só em parte dos provedores/modelos).
NOT_FOR_STRICT_MODE = {
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "pattern",
    "format",
    "default",
    "title",
}


@pytest.fixture(autouse=True)
def _without_openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENAI_* de quem roda os testes (fallback, base URL...) não mudam o que os testes veem."""
    for name in [name for name in os.environ if name.startswith("OPENAI_")]:
        monkeypatch.delenv(name)


class FakeOpenAI:
    """Responde na ordem dada e guarda cada requisição recebida."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx2.Response):
            return reply
        return httpx2.Response(200, json=reply)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def generator(server: FakeOpenAI, **env: str) -> OpenAIPlanGenerator:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        LLM_MODE="openai",
        OPENAI_API_KEY="sk-test",
        OPENAI_MODEL="gpt-test",
        **env,
    )
    client = httpx2.Client(transport=httpx2.MockTransport(server))
    return OpenAIPlanGenerator.from_settings(settings, http_client=client)


def reply(
    plan: dict[str, Any] | str | None = None,
    *,
    model: str = "gpt-test-2026-10-01",
    response_id: str = "resp_1",
    tokens: tuple[int, int] = (1200, 300),
    status: str = "completed",
    refusal: str | None = None,
    incomplete: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Corpo de uma resposta da Responses API com o plano (ou a recusa) como saída."""
    text = plan if isinstance(plan, str) else json.dumps(plan, ensure_ascii=False)
    content = (
        [{"type": "refusal", "refusal": refusal}]
        if refusal
        else [{"type": "output_text", "text": text, "annotations": []}]
    )
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1_790_000_000,
        "status": status,
        "incomplete_details": {"reason": incomplete} if incomplete else None,
        "error": {"code": error, "message": "detalhe do provedor"} if error else None,
        "model": model,
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": content,
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": tokens[0],
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": tokens[1],
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": sum(tokens),
        },
    }


def recorded_by_hatchet(error: BaseException) -> str:
    """O que fica gravado no Hatchet quando a task falha: mensagem e traceback encadeado."""
    return "".join(traceback.format_exception(error))


def schema_nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Cada subschema de um JSON Schema (os mapas `properties` e `$defs` são só nomes)."""
    if isinstance(node, list):
        for item in node:
            yield from schema_nodes(item)
    elif isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for subschema in value.values():
                    yield from schema_nodes(subschema)
            elif isinstance(value, dict | list):
                yield from schema_nodes(value)


def test_request_asks_for_strict_structured_output_and_no_storage() -> None:
    server = FakeOpenAI(reply(DEMO.example_plan))

    result = generator(server).generate(DEMO, ANSWERS)

    (request,) = server.requests
    body = server.bodies[0]
    assert (request.method, request.url.path) == ("POST", "/v1/responses")
    assert "response_format" not in body
    text_format = body["text"]["format"]
    assert (text_format["type"], text_format["strict"]) == ("json_schema", True)
    assert body["store"] is False and body["model"] == "gpt-test"
    assert body["instructions"] == DEMO.instructions
    assert result.document == DEMO.example_plan


def test_schema_sent_is_lean_and_strict_for_nested_plan_models() -> None:
    spec = replace(DEMO, plan_model=NestedPlan, example_plan=NESTED_PLAN)
    server = FakeOpenAI(reply(NESTED_PLAN))

    result = generator(server).generate(spec, ANSWERS)

    schema = server.bodies[0]["text"]["format"]["schema"]
    nodes = list(schema_nodes(schema))
    objects = [node for node in nodes if node.get("type") == "object"]
    assert len(objects) >= 2
    for node in objects:
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    assert not [node for node in nodes if NOT_FOR_STRICT_MODE & node.keys()]
    assert not [node for node in nodes if "$ref" in node and len(node) > 1]
    first_step = schema["properties"]["primeiro_passo"]
    assert first_step["description"] == "o que fazer primeiro"
    assert first_step["properties"]["prazo"]["enum"] == ["semana", "mes"]
    assert result.document == NESTED_PLAN


def test_a_closed_list_with_double_quotes_leaves_the_schema_and_goes_to_the_description() -> None:
    """O modo estrito recusa `"` em `enum` e `const` (HTTP 400 `invalid_json_schema`): os valores
    saem do nó e passam à descrição, para o modelo ainda ver a lista."""
    schema = strict_schema(QuotedChoices)

    entregavel = schema["properties"]["entregavel"]
    assert "enum" not in entregavel and entregavel["type"] == "string"
    assert entregavel["description"] == (
        f"o entregável. Valores permitidos: {QUOTED}; Guia Simples; Plano A."
    )
    items = schema["properties"]["entregaveis"]["items"]  # nó sem descrição própria
    assert "enum" not in items and items["type"] == "string"
    assert items["description"] == f"Valores permitidos: {QUOTED}; Guia Simples."
    unico = schema["properties"]["unico"]
    assert "const" not in unico and "enum" not in unico
    assert unico["description"] == 'Valores permitidos: Só o "único".'
    assert not quoted_literals(schema)


def test_a_closed_list_without_double_quotes_keeps_its_enum() -> None:
    schema = strict_schema(QuotedChoices)

    prazo = schema["properties"]["prazo"]
    assert prazo["enum"] == ["semana", "mes"]
    assert "description" not in prazo


def test_the_values_taken_out_of_the_schema_are_still_enforced_by_the_validation() -> None:
    """Só o schema enviado à OpenAI muda; o modelo completo recusa o valor fora da lista, e a
    rodada de reparo corrige."""
    plan = {"entregavel": QUOTED, "entregaveis": [QUOTED], "unico": 'Só o "único"', "prazo": "mes"}
    assert QuotedChoices.model_validate(plan).entregavel == QUOTED

    for field, value in (("entregavel", "Outro"), ("entregaveis", ["Outro"]), ("unico", "Outro")):
        with pytest.raises(ValidationError):
            QuotedChoices.model_validate({**plan, field: value})


def quoted_literals(schema: Any) -> list[str]:
    """Cada valor de `enum` ou `const`, em qualquer nó do schema, que tem aspas duplas."""
    found: list[str] = []
    for node in schema_nodes(schema):
        values = [*node.get("enum", []), *([node["const"]] if "const" in node else [])]
        found += [value for value in values if isinstance(value, str) and '"' in value]
    return found


@pytest.mark.parametrize("slug", LEGACY_SLUGS)
def test_no_legacy_form_sends_a_double_quote_in_an_enum_or_const(slug: str) -> None:
    """O MCC da migration 0008 tem o entregável `Recurso ecossistema 21 "Exemplo"` numa lista
    fechada: no `develop` toda submissão dele caía em `llm_rejected`."""
    assert not quoted_literals(strict_schema(legacy_spec(slug).plan_model))


def test_the_request_of_the_mcc_carries_the_deliverables_in_the_description() -> None:
    mcc = legacy_spec("mcc")
    answers = legacy_definition("mcc")["examples"]["answers"]
    server = FakeOpenAI(reply(mcc.example_plan))

    generator(server).generate(mcc, mcc.answers_model.model_validate(answers))

    schema = server.bodies[0]["text"]["format"]["schema"]
    assert not quoted_literals(schema)
    items = schema["$defs"]["Phase"]["properties"]["entregaveis_irpf"]["items"]
    assert "enum" not in items
    assert items["description"].startswith("Valores permitidos: Recurso combo 1;")
    assert QUOTED in items["description"]


def test_only_answers_cited_in_the_prompt_reach_the_llm() -> None:
    server = FakeOpenAI(reply(DEMO.example_plan))

    generator(server).generate(DEMO, ANSWERS)

    sent = json.dumps(server.bodies[0], ensure_ascii=False)
    assert ANSWERS.objetivo_90_dias in sent
    assert ANSWERS.email not in sent and ANSWERS.nome not in sent


def test_result_reports_effective_model_tokens_repairs_and_response_id() -> None:
    server = FakeOpenAI(reply(DEMO.example_plan, model="gpt-test-2026-10-01", response_id="resp_9"))

    result = generator(server).generate(DEMO, ANSWERS)

    assert result.model == "gpt-test-2026-10-01"
    assert result.usage == {
        "model": "gpt-test-2026-10-01",
        "response_id": "resp_9",
        "repairs": 0,
        "fallback": False,
        "input_tokens": 1200,
        "output_tokens": 300,
        "total_tokens": 1500,
    }


def test_invalid_plan_gets_one_repair_round_with_the_validation_errors() -> None:
    broken = {**DEMO.example_plan, "resumo": "Resumo curto.", "acoes": ["Só uma ação."]}
    server = FakeOpenAI(
        reply(broken, response_id="resp_1", tokens=(1200, 300)),
        reply(DEMO.example_plan, response_id="resp_2", tokens=(1600, 320)),
    )

    result = generator(server).generate(DEMO, ANSWERS)

    first, second = server.bodies
    *conversation, previous, feedback = second["input"]
    assert conversation == first["input"]
    assert previous == {"role": "assistant", "content": json.dumps(broken, ensure_ascii=False)}
    assert feedback["role"] == "user"
    assert "resumo" in feedback["content"] and "acoes" in feedback["content"]
    assert "Resumo curto." not in feedback["content"]  # erros só com caminho e regra
    assert result.document == DEMO.example_plan
    assert (result.usage["repairs"], result.usage["response_id"]) == (1, "resp_2")
    assert result.usage["total_tokens"] == 3420


def test_repair_round_records_the_path_and_type_of_each_broken_rule_without_values() -> None:
    broken = {
        **DEMO.example_plan,
        "resumo": "Resumo curto.",
        "acoes": [f"Divulgar o {ANSWERS.escritorio}.", ANSWERS.objetivo_90_dias],
    }
    server = FakeOpenAI(reply(broken), reply(DEMO.example_plan))

    result = generator(server).generate(DEMO, ANSWERS)

    assert result.usage["repairs"] == 1
    assert result.usage["repair_reasons"] == ["resumo:string_too_short", "acoes:too_short"]
    stored = json.dumps(result.usage, ensure_ascii=False)
    for value in ("Resumo curto.", ANSWERS.escritorio, ANSWERS.objetivo_90_dias):
        assert value not in stored


class CheckedPlan(NestedPlan):
    """Plano com uma regra do plano inteiro, cuja mensagem cita o valor recebido."""

    @model_validator(mode="after")
    def _first_step_not_repeated(self) -> CheckedPlan:
        if self.primeiro_passo in self.passos:
            raise ValueError(f"primeiro passo repetido nos passos: {self.primeiro_passo.acao}")
        return self


ECHOING_STEP = {"acao": f"Divulgar o {ANSWERS.escritorio}", "prazo": "semana"}
# Chaves que o plano não tem; só dígitos (um telefone) também não é índice de lista.
EXTRA_KEYS = {ANSWERS.escritorio: "sim", "11912345678": "telefone"}


@pytest.mark.parametrize(
    ("output", "reasons"),
    [
        (
            {**NESTED_PLAN, "passos": [ECHOING_STEP, {"acao": "Ir", "prazo": ANSWERS.escritorio}]},
            ["passos.1.acao:string_too_short", "passos.1.prazo:literal_error"],
        ),
        (
            {**NESTED_PLAN, "passos": [ECHOING_STEP, {**ECHOING_STEP, **EXTRA_KEYS}]},
            ["passos.1.*:extra_forbidden"] * 2,
        ),
        (
            {**NESTED_PLAN, "primeiro_passo": ECHOING_STEP, "passos": [ECHOING_STEP] * 2},
            ["plano:value_error"],
        ),
        (f'{{"titulo": "Plano do {ANSWERS.escritorio}', ["plano:json_invalid"]),
        (
            {**NESTED_PLAN, **{f"{ANSWERS.escritorio} {n}": n for n in range(25)}},
            ["*:extra_forbidden"] * 20,
        ),
    ],
    ids=["nested_fields", "key_outside_the_plan", "plan_rule", "invalid_json", "at_most_20"],
)
def test_repair_reasons_keep_only_the_path_and_the_error_type(
    output: dict[str, Any] | str, reasons: list[str]
) -> None:
    """Valor recebido, mensagem do erro e chave que o plano não tem (o modo estrito impede, mas
    o provedor pode falhar nisso) podem ecoar o lead: nenhum deles vai para o `usage`."""
    spec = replace(DEMO, plan_model=CheckedPlan, example_plan=NESTED_PLAN)
    server = FakeOpenAI(reply(output), reply(NESTED_PLAN))

    result = generator(server).generate(spec, ANSWERS)

    assert (result.usage["repairs"], result.usage["repair_reasons"]) == (1, reasons)
    assert ANSWERS.escritorio not in json.dumps(result.usage, ensure_ascii=False)


def test_plan_still_invalid_after_the_repair_is_a_retryable_plan_invalid() -> None:
    echoing_the_lead = {**DEMO.example_plan, "acoes": [f"Divulgar o {ANSWERS.escritorio}."]}
    server = FakeOpenAI(reply(echoing_the_lead), reply(echoing_the_lead))

    with pytest.raises(RetryableError) as caught:
        generator(server).generate(DEMO, ANSWERS)

    assert caught.value.code is FailureCode.PLAN_INVALID
    assert len(server.requests) == 2
    assert ANSWERS.escritorio not in recorded_by_hatchet(caught.value)


class RecordingCheck:
    """`check_plan` de teste: guarda o que recebeu e devolve os problemas dados, um por chamada."""

    def __init__(self, *problems: list[str]) -> None:
        self.problems = list(problems)
        self.calls: list[tuple[BaseModel, BaseModel]] = []

    def __call__(self, plan: BaseModel, answers: BaseModel) -> list[str]:
        self.calls.append((plan, answers))
        return self.problems.pop(0)


def test_form_check_problems_take_the_one_repair_round() -> None:
    check = RecordingCheck(["acoes.0: comece pela ação desta semana."], [])
    server = FakeOpenAI(reply(DEMO.example_plan), reply(DEMO.example_plan))

    result = generator(server).generate(replace(DEMO, check_plan=check), ANSWERS)

    assert result.usage["repairs"] == 1
    feedback = server.bodies[1]["input"][-1]["content"]
    assert "- plano: acoes.0: comece pela ação desta semana." in feedback
    assert [(type(plan), answers) for plan, answers in check.calls] == [
        (DEMO.plan_model, ANSWERS)
    ] * 2


def test_form_check_only_sees_plans_the_plan_model_accepts() -> None:
    check = RecordingCheck([])
    server = FakeOpenAI(reply({**DEMO.example_plan, "acoes": []}), reply(DEMO.example_plan))

    result = generator(server).generate(replace(DEMO, check_plan=check), ANSWERS)

    assert result.usage["repairs"] == 1
    assert len(check.calls) == 1


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (429, RetryableError, FailureCode.LLM_UNAVAILABLE),
        (500, RetryableError, FailureCode.LLM_UNAVAILABLE),
        (503, RetryableError, FailureCode.LLM_UNAVAILABLE),
        (408, RetryableError, FailureCode.LLM_UNAVAILABLE),
        (409, RetryableError, FailureCode.LLM_UNAVAILABLE),
        (400, TerminalError, FailureCode.LLM_REJECTED),
        (401, TerminalError, FailureCode.LLM_REJECTED),
        (403, TerminalError, FailureCode.LLM_REJECTED),
        (404, TerminalError, FailureCode.LLM_REJECTED),
        (422, TerminalError, FailureCode.LLM_REJECTED),
    ],
)
def test_http_errors_are_classified_and_not_retried_by_the_sdk(
    status: int, error: type[Exception], code: FailureCode
) -> None:
    body = {"error": {"message": f"eco: {ANSWERS.objetivo_90_dias}", "type": "x", "code": "y"}}
    server = FakeOpenAI(httpx2.Response(status, json=body, headers={"x-request-id": "req_42"}))

    with pytest.raises(error) as caught:
        generator(server).generate(DEMO, ANSWERS)

    assert caught.value.code is code  # type: ignore[attr-defined]
    assert len(server.requests) == 1  # quem repete é o Hatchet
    assert f"HTTP {status}" in str(caught.value) and "req_42" in str(caught.value)
    assert ANSWERS.objetivo_90_dias not in recorded_by_hatchet(caught.value)


def test_exhausted_quota_is_terminal_because_retrying_cannot_help() -> None:
    body = {
        "error": {"message": "quota", "type": "insufficient_quota", "code": "insufficient_quota"}
    }
    server = FakeOpenAI(httpx2.Response(429, json=body))

    with pytest.raises(TerminalError) as caught:
        generator(server, OPENAI_FALLBACK_MODEL="gpt-reserva").generate(DEMO, ANSWERS)

    assert caught.value.code is FailureCode.LLM_REJECTED
    assert "insufficient_quota" in str(caught.value)
    assert len(server.requests) == 1  # nem o modelo reserva: a cota é da conta


@pytest.mark.parametrize(
    ("env", "timeout"),
    [
        ({}, {"connect": 10.0, "read": 120.0, "write": 120.0, "pool": 120.0}),
        (
            {"OPENAI_TIMEOUT_SECONDS": "42", "OPENAI_CONNECT_TIMEOUT_SECONDS": "3"},
            {"connect": 3.0, "read": 42.0, "write": 42.0, "pool": 42.0},
        ),
    ],
    ids=["defaults", "from_env"],
)
def test_connecting_has_a_short_timeout_of_its_own(
    env: dict[str, str], timeout: dict[str, float]
) -> None:
    server = FakeOpenAI(reply(DEMO.example_plan))

    generator(server, **env).generate(DEMO, ANSWERS)

    # Sem limite próprio, cada endereço IPv6 de uma rota quebrada esperava o timeout da chamada.
    assert server.requests[0].extensions["timeout"] == timeout


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectTimeout("timed out"),
        httpx2.ReadTimeout("timed out"),
        httpx2.ConnectError("off"),
    ],
    ids=["connect_timeout", "read_timeout", "connect_error"],
)
def test_timeouts_and_connection_failures_are_retryable(failure: Exception) -> None:
    server = FakeOpenAI(failure)

    with pytest.raises(RetryableError) as caught:
        generator(server, OPENAI_TIMEOUT_SECONDS="42").generate(DEMO, ANSWERS)

    assert caught.value.code is FailureCode.LLM_UNAVAILABLE
    assert len(server.requests) == 1
    assert server.requests[0].extensions["timeout"]["read"] == 42.0


TRUNCATED = '{"titulo": "Plano de 90 dias", "resumo": "O escritório'


@pytest.mark.parametrize(
    ("body", "error", "code"),
    [
        (reply(refusal=f"Não posso, {ANSWERS.nome}."), TerminalError, FailureCode.LLM_REJECTED),
        (
            reply(TRUNCATED, status="incomplete", incomplete="max_output_tokens"),
            RetryableError,
            FailureCode.PLAN_INVALID,
        ),
        (
            reply(TRUNCATED, status="incomplete", incomplete="content_filter"),
            TerminalError,
            FailureCode.LLM_REJECTED,
        ),
        (
            reply("", status="failed", error="server_error"),
            RetryableError,
            FailureCode.LLM_UNAVAILABLE,
        ),
    ],
    ids=["refusal", "incomplete", "content_filter", "failed"],
)
def test_refused_or_unfinished_responses_are_classified_without_repair(
    body: dict[str, Any], error: type[Exception], code: FailureCode
) -> None:
    server = FakeOpenAI(body)

    with pytest.raises(error) as caught:
        generator(server).generate(DEMO, ANSWERS)

    assert caught.value.code is code  # type: ignore[attr-defined]
    assert len(server.requests) == 1
    assert ANSWERS.nome not in recorded_by_hatchet(caught.value)


def test_retryable_failure_of_the_primary_model_tries_the_fallback_once() -> None:
    server = FakeOpenAI(
        httpx2.Response(503, json={"error": {"message": "sobrecarregado"}}),
        reply(DEMO.example_plan, model="gpt-reserva-2026-09-01"),
    )

    result = generator(server, OPENAI_FALLBACK_MODEL="gpt-reserva").generate(DEMO, ANSWERS)

    assert [body["model"] for body in server.bodies] == ["gpt-test", "gpt-reserva"]
    assert result.model == "gpt-reserva-2026-09-01"
    assert (result.usage["fallback"], result.usage["repairs"]) == (True, 0)
    assert result.document == DEMO.example_plan


def test_repair_reasons_are_those_of_the_model_that_wrote_the_plan() -> None:
    """Como `repairs`, os motivos são do modelo que escreveu o plano: os do principal, que caiu
    no meio do reparo, não se misturam aos do reserva."""
    server = FakeOpenAI(
        reply({**DEMO.example_plan, "acoes": []}),
        httpx2.Response(503, json={}),
        reply({**DEMO.example_plan, "resumo": "Resumo curto."}),
        reply(DEMO.example_plan),
    )

    result = generator(server, OPENAI_FALLBACK_MODEL="gpt-reserva").generate(DEMO, ANSWERS)

    assert [body["model"] for body in server.bodies] == ["gpt-test"] * 2 + ["gpt-reserva"] * 2
    assert (result.usage["fallback"], result.usage["repairs"]) == (True, 1)
    assert result.usage["repair_reasons"] == ["resumo:string_too_short"]


def test_a_failing_fallback_never_turns_a_retryable_failure_into_a_terminal_one() -> None:
    server = FakeOpenAI(
        httpx2.Response(503, json={}, headers={"x-request-id": "req_principal"}),
        httpx2.Response(404, json={"error": {"code": "model_not_found"}}),
    )

    with pytest.raises(RetryableError) as caught:
        generator(server, OPENAI_FALLBACK_MODEL="gpt-reserva").generate(DEMO, ANSWERS)

    assert caught.value.code is FailureCode.LLM_UNAVAILABLE
    recorded = recorded_by_hatchet(caught.value)
    assert "req_principal" in recorded and "model_not_found" in recorded


def test_terminal_failure_of_the_primary_model_skips_the_fallback() -> None:
    server = FakeOpenAI(httpx2.Response(401, json={}))

    with pytest.raises(TerminalError):
        generator(server, OPENAI_FALLBACK_MODEL="gpt-reserva").generate(DEMO, ANSWERS)

    assert len(server.requests) == 1


def test_openai_mode_builds_the_openai_generator_offline() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, LLM_MODE="openai", OPENAI_API_KEY="sk-test", OPENAI_MODEL="gpt-test"
    )

    assert isinstance(build_llm(settings), OpenAIPlanGenerator)


def claimed_job(deps: Deps) -> Job:
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
    return job


def test_plan_step_in_openai_mode_stores_the_validated_plan_and_usage(deps: Deps) -> None:
    server = FakeOpenAI(reply(DEMO.example_plan, model="gpt-test-2026-10-01", response_id="resp_7"))
    deps.llm = generator(server)
    job = claimed_job(deps)

    plan_id, reused = pipeline.generate_plan(deps, job.id, 1)

    with deps.sessions() as session:
        plan = session.get_one(Plan, plan_id)
    assert not reused and plan.document == DEMO.example_plan
    assert (plan.model, plan.usage["response_id"]) == ("gpt-test-2026-10-01", "resp_7")
    sent = json.dumps(server.bodies, ensure_ascii=False)
    assert "contadora@example.test" not in sent and "Contadora Exemplo" not in sent


def test_plan_step_in_openai_mode_stores_the_repair_reasons_in_the_usage(deps: Deps) -> None:
    broken = {**DEMO.example_plan, "acoes": [f"Divulgar o {ANSWERS.escritorio}."]}
    deps.llm = generator(FakeOpenAI(reply(broken), reply(DEMO.example_plan)))
    job = claimed_job(deps)

    plan_id, _ = pipeline.generate_plan(deps, job.id, 1)

    with deps.sessions() as session:
        usage = session.get_one(Plan, plan_id).usage
    assert (usage["repairs"], usage["repair_reasons"]) == (1, ["acoes:too_short"])
    assert ANSWERS.escritorio not in json.dumps(usage, ensure_ascii=False)


def test_plan_step_in_openai_mode_stores_nothing_when_the_plan_stays_invalid(deps: Deps) -> None:
    broken = {**DEMO.example_plan, "acoes": []}
    deps.llm = generator(FakeOpenAI(reply(broken), reply(broken)))
    job = claimed_job(deps)

    with pytest.raises(RetryableError) as caught:
        pipeline.generate_plan(deps, job.id, 1)

    assert caught.value.code is FailureCode.PLAN_INVALID
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Plan)) == 0
