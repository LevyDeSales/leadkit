"""Contato de um pedido: respostas persistidas e versão exata, nunca o cadastro compartilhado."""

from __future__ import annotations

from pydantic import ValidationError

from app.errors import FailureCode, TerminalError
from app.forms import FormRegistry, FormSpec, LeadIdentity, UnknownForm
from app.models import Submission


def identity_from_answers(submission: Submission, spec: FormSpec) -> LeadIdentity:
    """Revalida o envio com seu contrato imutável. Falha sem dados pessoais nem contato presumido."""
    if spec.slug != submission.form_slug or spec.version_number != submission.form_version_number:
        raise TerminalError(FailureCode.INTERNAL, "versão divergente para a identidade do envio")
    try:
        answers = spec.answers_model.model_validate(submission.answers)
        return spec.identity(answers)
    except ValidationError:
        raise TerminalError(
            FailureCode.INTERNAL, "respostas não validam com a versão do formulário do envio"
        ) from None


def submission_form(forms: FormRegistry, submission: Submission) -> FormSpec:
    """Resolve somente a versão histórica; a falta dela é uma falha controlada e sem dados pessoais."""
    try:
        spec = forms.get(submission.form_slug, submission.form_version_number)
    except UnknownForm:
        raise TerminalError(
            FailureCode.INTERNAL, "versão do formulário indisponível para a identidade do envio"
        ) from None
    return spec


def resolve_identity(forms: FormRegistry, submission: Submission) -> LeadIdentity:
    """Não procura versão atual nem usa a lead se a versão histórica estiver indisponível."""
    return identity_from_answers(submission, submission_form(forms, submission))
