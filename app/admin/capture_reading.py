"""Apresentação das respostas com os rótulos da versão imutável do envio."""

import json
from collections.abc import Mapping
from typing import Any

from app.forms import FormSpec


def answer_rows(spec: FormSpec | None, answers: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Mantém a ordem histórica das perguntas e a ordem respondida das alternativas."""
    questions = {
        question.name: question
        for step in (spec.screen.steps if spec and spec.screen else ())
        for question in step.questions
    }
    rows = []
    for name in dict.fromkeys([*questions, *answers]):
        question = questions.get(name)
        value = answers.get(name)
        available = question is not None
        if value is None:
            text = "Não respondida"
        elif question and question.options:
            labels = {option.value: option.label for option in question.options}
            values = value if isinstance(value, list) else [value]
            parts = []
            for item in values:
                if isinstance(item, str) and item in labels:
                    parts.append(str(labels[item]))
                else:
                    available = False
                    parts.append(f"{_value_text(item)} (rótulo indisponível)")
            text = "; ".join(parts)
            if not values:
                text = "Nenhuma alternativa selecionada"
        else:
            text = _value_text(value) if value != "" else "Resposta vazia"
        rows.append(
            {
                "name": str(name),
                "label": str(question.label) if question else str(name),
                "value_text": text,
                "historical_label_available": available,
            }
        )
    return rows


def _value_text(value: Any) -> str:
    """Conserva os valores JSON sem transformar zero ou falso em ausência."""
    return str(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
