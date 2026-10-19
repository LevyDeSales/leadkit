from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from app.forms import FormSpec


@dataclass(frozen=True)
class LlmResult:
    document: dict[str, Any]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


class PlanGenerator(Protocol):
    def generate(self, spec: FormSpec, answers: BaseModel) -> LlmResult:
        """Devolve o plano já validado contra `spec.plan_model`.

        Levanta `RetryableError`/`TerminalError` (app.errors) em falhas do provedor."""
        ...


class FakePlanGenerator:
    """Devolve o plano de exemplo do formulário. Só para desenvolvimento e testes."""

    def generate(self, spec: FormSpec, answers: BaseModel) -> LlmResult:
        spec.render_prompt(answers)  # garante que o template do prompt renderiza com as respostas
        plan = spec.plan_model.model_validate(spec.example_plan)
        return LlmResult(document=plan.model_dump(mode="json"), model="fake", usage={"fake": True})
