"""O modelo de entrada das prévias do PDF e da página de resultado: o que
`render_form_draft_pdf_preview` e `render_form_draft_result_preview` recebem.

As respostas não são JSON (o PDF é `application/pdf` e as páginas são `text/html`), então só há modelo de
entrada. Os nomes dos campos são os do documento do rascunho (`get_form_draft`): `pdf.html`, `pdf.css`,
`pdf.scores` e `preview`, para o caminho de um erro de template (`errors[].field`) ser também o de quem
manda. **Nada aqui é dado de lead**: o plano e as respostas da prévia são os de exemplo do rascunho ou os
do último teste.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.admin.actions import previews
from app.admin.api.common import ApiInput
from app.forms import draft_previews
from app.forms.definition.document import MAX_SCORES, Name

SOURCE_TEXT = (
    "O plano que a prévia mostra. `example` (o padrão): o plano de exemplo do rascunho "
    "(`examples.plan`), com as respostas de exemplo. `test`: o plano do último teste do rascunho que "
    "terminou com sucesso (`list_form_draft_tests`), com as respostas dele; sem um teste assim, ou se o "
    "modelo do plano do rascunho já não o aceita, a resposta é `409 state_conflict`."
)
OMITTED_TEXT = " Ausente: o que o rascunho salvo já tem."


class PdfTemplatesChange(ApiInput):
    """O PDF como a prévia o usa: o HTML, o CSS e as notas que ele pode mostrar. O que a chamada deixa
    de fora é o do rascunho salvo."""

    html: str | None = Field(
        default=None,
        description="O HTML do PDF (um template Jinja, o campo `pdf.html` do documento). Só pode pedir "
        "arquivos do app, as imagens do formulário (`asset:<nome>`) e `data:`; qualquer outro recurso "
        "é `422` no campo `pdf.html`, com a linha." + OMITTED_TEXT,
        examples=["<h1>{{ plan.titulo }}</h1>"],
    )
    css: str | None = Field(
        default=None,
        description="O CSS do PDF (o campo `pdf.css` do documento), com a mesma regra dos recursos "
        "que o HTML." + OMITTED_TEXT,
        examples=["h1 { color: #0b3d2e; }"],
    )
    scores: list[Name] | None = Field(
        default=None,
        max_length=MAX_SCORES,
        description="As notas da pontuação que o PDF e a prévia podem mostrar em `scores` (o campo "
        "`pdf.scores` do documento): os nomes das notas de `scoring`. Uma lista vazia não libera nenhuma. "
        "Um nome que não é uma nota da pontuação é `422` em `pdf.scores.<n>`." + OMITTED_TEXT,
        examples=[["porte"]],
    )


class PreviewDraftRequest(ApiInput):
    """O que a prévia usa no lugar do rascunho salvo, **sem gravar nada**. Tudo é opcional: sem corpo
    (ou `{}`), vale o rascunho salvo, com o plano de exemplo. **Mandar um template ou as notas é só do
    papel `total`** (`pdf.html`, `pdf.css`, `pdf.scores`, `preview`): compilar e renderizar um texto que
    quem chama escreveu custa CPU do processo que serve as leads, e a chave `leitura` só pré-visualiza o
    que já está salvo."""

    pdf: PdfTemplatesChange | None = Field(
        default=None,
        description="O PDF que a prévia usa no lugar do salvo, campo a campo (`html`, `css`, "
        "`scores`)." + OMITTED_TEXT,
    )
    preview: str | None = Field(
        default=None,
        description="O template da página de resultado (o campo `preview` do documento) que a prévia "
        "usa no lugar do salvo. O PDF e a página dividem o mesmo contrato: um erro em qualquer um dos "
        "templates (`pdf.html`, `pdf.css`, `preview`) recusa as duas prévias." + OMITTED_TEXT,
        examples=["<section class='section'><h2>{{ plan.titulo }}</h2></section>"],
    )
    source: Literal["example", "test"] = Field(
        default=draft_previews.EXAMPLE, description=SOURCE_TEXT, examples=["example"]
    )

    def changes(self) -> previews.TemplateChanges:
        """O que a prévia troca no rascunho salvo, como a ação o quer."""
        pdf = self.pdf or PdfTemplatesChange()
        return previews.TemplateChanges(
            pdf_html=pdf.html,
            pdf_css=pdf.css,
            preview=self.preview,
            scores=None if pdf.scores is None else tuple(pdf.scores),
        )

    @property
    def sends_templates(self) -> bool:
        """Se o corpo manda um template ou as notas (`pdf.html`, `pdf.css`, `pdf.scores`, `preview`)
        para a prévia compilar e renderizar no lugar dos salvos. Um corpo só com `source`, ou com um
        `pdf` vazio, não manda nada: vale o rascunho salvo, que já compilou quando foi salvo."""
        sent = self.changes()
        return any(
            value is not None for value in (sent.pdf_html, sent.pdf_css, sent.preview, sent.scores)
        )
