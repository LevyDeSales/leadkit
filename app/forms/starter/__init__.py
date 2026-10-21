"""O modelo inicial de um formulário criado do zero no admin: uma definição completa e pronta para
gerar plano, na marca da Leadkit. Esta pasta só guarda as peças do modelo, que a
operadora muda depois no rascunho.

- `definition.json`: a página, as etapas e as perguntas, a identidade, o consentimento (sem a
  versão), o modelo do plano (título, resumo, três fases com ações e entrega, próximo passo e
  mensagem final) e os exemplos, fictícios;
- `instructions.md`: o papel e as regras do LLM, com a regra de não prometer resultado;
- `prompt.md`: o fim do prompt, depois das respostas, que `prompt_of` lista uma a uma;
- `pdf.html`, `pdf.css` e `preview.html`: o PDF na marca (capa, ponto de partida, fases e fecho)
  e a prévia da página de resultado.

`definition(slug=..., title=...)` monta a definição com o endereço e o título do formulário novo; a
versão do texto do consentimento é `<slug>-v1`, a da primeira publicação.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from app.forms import CONTACT_KINDS
from app.forms.definition.questions import CHOICE_KINDS

FOLDER = Path(__file__).parent
PROMPT_HEAD = "Diagnóstico do projeto, com as respostas da participante:\n\n"
UNANSWERED = "não respondeu"
# Os delimitadores do Jinja: um rótulo com eles vai ao prompt como texto, sem virar template.
_JINJA_MARKS = ("{{", "}}", "{%", "%}", "{#", "#}")


def _read(name: str) -> str:
    return (FOLDER / name).read_text(encoding="utf-8")


def _literal(text: str) -> str:
    """O texto como ele é, numa linha, dentro do template do prompt."""
    line = " ".join(text.split())
    if any(mark in line for mark in _JINJA_MARKS):
        return "{% raw %}" + line.replace("endraw", "end raw") + "{% endraw %}"
    return line


def _contact_names(identity: Mapping[str, Any]) -> set[str]:
    return {name for name in identity.values() if isinstance(name, str)}


def prompt_of(steps: Iterable[Mapping[str, Any]], identity: Mapping[str, Any]) -> str:
    """O prompt do modelo inicial para estas perguntas: cada resposta que não é dado de contato,
    com a pergunta, e o fim do prompt (`prompt.md`). O rótulo das opções vai por `labels`, e o
    texto livre da lead, entre aspas triplas, que as instruções tratam como dado."""
    contact = _contact_names(identity)
    lines: list[str] = []
    for step in steps:
        for question in step.get("questions", ()):
            name, kind = question["name"], question["kind"]
            if question.get("contact") or name in contact or kind in CONTACT_KINDS:
                continue
            required = question.get("required", True)
            lines.append(f"- Pergunta: {_literal(question['label'])}")
            if kind in CHOICE_KINDS:
                value = f"labels.{name}" if required else f'labels.{name} or "{UNANSWERED}"'
                lines.append(f"  Resposta: {{{{ {value} }}}}")
                continue
            value = f"answers.{name}" if required else f'answers.{name} or "{UNANSWERED}"'
            if kind == "textarea":
                lines += ["  Resposta, nas palavras dela:", '"""', f"{{{{ {value} }}}}", '"""']
            else:
                lines.append(f'  Resposta: """{{{{ {value} }}}}"""')
    return PROMPT_HEAD + "\n".join(lines) + "\n\n" + _read("prompt.md")


def definition(*, slug: str, title: str, generation: str = "pdf") -> dict[str, Any]:
    """A definição do modelo inicial para um formulário novo, com este endereço e este título."""
    base = json.loads(_read("definition.json"))
    document = {
        "format": 1,
        "slug": slug,
        "title": title,
        "intro": base["intro"],
        "minutes": base["minutes"],
        "steps": base["steps"],
        "identity": base["identity"],
        "consent": {"version": f"{slug}-v1", **base["consent"]},
        "instructions": _read("instructions.md"),
        "prompt": prompt_of(base["steps"], base["identity"]),
        "plan": base["plan"],
        "pdf": {"html": _read("pdf.html"), "css": _read("pdf.css")},
        "preview": _read("preview.html"),
        "data": {},
        "examples": base["examples"],
    }

    if generation == "none":
        for key in ("instructions", "prompt", "plan", "pdf", "preview"):
            document.pop(key)
        document["examples"].pop("plan", None)
        document["steps"][0]["help"] = (
            "Estes dados identificam suas respostas. Você pode deixar os campos opcionais em branco."
        )
        document["generation"] = "none"
        document["completion"] = {"mode": "confirmation"}
        document["intro"] = "Conte suas respostas. Elas serão salvas ao concluir o envio."
        document["consent"]["processing"] = (
            "Concordo com o tratamento das minhas respostas para esta pesquisa."
        )
        document["consent"]["notice"] = (
            "A operadora armazena suas respostas e contato. Consulte a política da instalação para conhecer os prazos e como solicitar a exclusão dos dados."
        )
    return document
