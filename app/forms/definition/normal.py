"""A forma normal de uma definição: o que ela diz, sem o que o formato dispensa.

Uma definição importada pode trazer o padrão do formato por extenso (`"required": true`,
`"contact": false`, `"whatsapp": null`), e o editor estruturado do admin só escreve o que difere do
padrão. As duas dizem a mesma coisa, mas o JSON delas é outro: a impressão digital muda, e uma
comparação linha a linha mostra "mudanças" que não mudam nada. A forma normal tira de cada parte o
que é igual ao padrão e as chaves vazias que o formato dispensa; duas definições com a mesma forma
normal compilam para o mesmo contrato.

Quem a usa: o rascunho (`Draft.pending`, `drafts.pending_slugs` e o salvar, que tira o rascunho
quando ele volta a dizer o mesmo que a versão atual) e a comparação entre versões
(`app.forms.definition.changes.compare`). Ela **não** entra na impressão digital do que vai ao LLM
(`changes.llm_fingerprint`): essa continua lendo o documento como está, porque os testes e as
publicações já guardam a impressão de hoje.

Uma parte que o formato atual não lê (uma versão antiga que não compila mais) fica como está.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.forms.definition.document import (
    ConsentDoc,
    DataBlockDoc,
    IdentityDoc,
    Label,
    Name,
    NoticesDoc,
    PdfDoc,
    PhasesDoc,
    PlanDoc,
    ScoringDoc,
    StepDoc,
    Text,
    fingerprint,
)

# O que cada parte da definição diz, com os padrões do formato.
PARTS: dict[str, TypeAdapter[Any]] = {
    "title": TypeAdapter(Label),
    "intro": TypeAdapter(Text),
    "minutes": TypeAdapter(int),
    "steps": TypeAdapter(list[StepDoc]),
    "identity": TypeAdapter(IdentityDoc),
    "consent": TypeAdapter(ConsentDoc),
    "plan": TypeAdapter(PlanDoc),
    "phases": TypeAdapter(PhasesDoc),
    "data": TypeAdapter(dict[Name, DataBlockDoc]),
    "scoring": TypeAdapter(ScoringDoc),
    "notices": TypeAdapter(NoticesDoc),
    "pdf": TypeAdapter(PdfDoc),
}
# As partes opcionais: ausentes, nulas ou vazias, dizem o mesmo (nada).
OPTIONAL_PARTS = ("scoring", "phases", "notices", "data")


def part(key: str, value: Any) -> Any:
    """A parte `key` da definição na forma normal; None se ela não é uma das partes
    (`PARTS`) ou o formato atual não a lê."""
    adapter = PARTS.get(key)
    if adapter is None:
        return None
    try:
        return adapter.dump_python(
            adapter.validate_python(value), mode="json", exclude_defaults=True
        )
    except ValidationError:
        return None


def answered(answers: Any) -> Any:
    """As respostas de exemplo sem as que ficaram em branco (vale como não respondida)."""
    if not isinstance(answers, Mapping):
        return answers
    return {k: v for k, v in answers.items() if v is not None and v != "" and v != []}


def normalized(document: Mapping[str, Any]) -> dict[str, Any]:
    """A definição na forma normal: cada parte sem os padrões escritos por extenso, as partes
    opcionais vazias ausentes e as respostas de exemplo sem as em branco."""
    found = dict(document)
    for key in PARTS:
        if key in found and (minimal := part(key, found[key])) is not None:
            found[key] = minimal
    for key in OPTIONAL_PARTS:
        if key in found and found[key] in (None, {}):
            del found[key]
    examples = found.get("examples")
    if isinstance(examples, Mapping) and "answers" in examples:
        found["examples"] = {**examples, "answers": answered(examples["answers"])}
    return found


def same_meaning(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """As duas definições dizem o mesmo: têm a mesma forma normal."""
    return fingerprint(normalized(first)) == fingerprint(normalized(second))
