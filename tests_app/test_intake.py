from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, DEMO_CONSENT
from pydantic import ValidationError

from app.deps import Deps
from app.forms import FormSpec
from app.intake import (
    Accepted,
    ConsentTextMismatch,
    IdempotencyConflict,
    QueueFull,
    SubmissionIn,
    SubmissionLimitExceeded,
    SubmissionLimits,
    accept_submission,
)
from app.models import Job, Lead, Submission


def submit(
    deps: Deps,
    key: str = "k1",
    answers: dict[str, Any] | None = None,
    *,
    form: FormSpec | None = None,
    limits: SubmissionLimits | None = None,
    now: datetime | None = None,
    **extra: Any,
) -> Accepted:
    body = SubmissionIn.model_validate(
        {"answers": answers or DEMO_ANSWERS, "consent": DEMO_CONSENT, **extra}
    )
    with deps.sessions.begin() as session:
        return accept_submission(
            session,
            form or deps.forms.current("demo"),
            body,
            idempotency_key=key,
            signer=deps.signer,
            link_ttl=deps.link_ttl,
            limits=limits or deps.submission_limits,
            now=now,
        )


def lead_answers(number: int) -> dict[str, Any]:
    return {**DEMO_ANSWERS, "email": f"lead{number}@example.test"}


def count(deps: Deps, model: type) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(model)) or 0


def test_valid_submission_creates_lead_submission_and_queued_job(deps: Deps) -> None:
    accepted = submit(deps, tracking={"utm_source": "instagram", "nao_permitido": "x"})

    assert not accepted.replay
    assert accepted.job.status == "queued" and accepted.job.attempt == 1
    deps.signer.verify(accepted.token, accepted.job)
    with deps.sessions() as session:
        submission = session.scalars(sa.select(Submission)).one()
        lead = session.get_one(Lead, submission.lead_id)
    assert lead.email == "contadora@example.test"
    assert submission.tracking == {"utm_source": "instagram"}
    assert submission.priority == "P2" and submission.scores == {"porte": 50}
    assert submission.consent["text_version"] == "demo-v1"


def test_public_id_is_random_and_not_sequential(deps: Deps) -> None:
    first = submit(deps, key="a").job.public_id
    second = submit(deps, key="b").job.public_id
    assert first != second and len(first) >= 16 and not first.startswith("job-")


def test_privacy_consent_is_mandatory_and_nothing_is_stored(deps: Deps) -> None:
    with pytest.raises(ValidationError):
        SubmissionIn.model_validate(
            {"answers": DEMO_ANSWERS, "consent": {"privacy": False, "text_version": "demo-v1"}}
        )
    with pytest.raises(ValidationError):
        SubmissionIn.model_validate(
            {"answers": DEMO_ANSWERS, "consent": {"text_version": "demo-v1"}}
        )
    assert count(deps, Submission) == 0


def test_client_cannot_send_schema_or_extra_fields(deps: Deps) -> None:
    with pytest.raises(ValidationError):
        SubmissionIn.model_validate(
            {
                "answers": DEMO_ANSWERS,
                "consent": DEMO_CONSENT,
                "schema_json": {},
                "consent_required": False,
            }
        )


def test_invalid_answers_are_rejected_without_rows(deps: Deps) -> None:
    with pytest.raises(ValidationError):
        submit(deps, answers={**DEMO_ANSWERS, "tamanho_equipe": "gigante"})
    with pytest.raises(ValidationError):
        submit(deps, answers={**DEMO_ANSWERS, "email": "nao-e-email"})
    with pytest.raises(ValidationError):
        submit(deps, answers={**DEMO_ANSWERS, "campo_inventado": "x"})
    assert count(deps, Lead) == 0 and count(deps, Job) == 0


def test_outdated_consent_text_is_a_conflict(deps: Deps) -> None:
    body = SubmissionIn.model_validate(
        {"answers": DEMO_ANSWERS, "consent": {"privacy": True, "text_version": "versao-antiga"}}
    )
    with deps.sessions.begin() as session, pytest.raises(ConsentTextMismatch):
        accept_submission(
            session,
            deps.forms.current("demo"),
            body,
            idempotency_key="k",
            signer=deps.signer,
            link_ttl=deps.link_ttl,
            limits=deps.submission_limits,
        )


def marketing(**channels: bool) -> dict[str, Any]:
    return {"consent": {**DEMO_CONSENT, **channels}}


def test_requested_marketing_channel_is_accepted_and_recorded(deps: Deps) -> None:
    submit(deps, **marketing(marketing_email=True))  # o demo pede o canal e-mail

    with deps.sessions() as session:
        consent = session.scalars(sa.select(Submission)).one().consent
    assert consent["marketing_email"] is True and consent["marketing_whatsapp"] is False


def test_marketing_channel_the_form_does_not_ask_for_is_refused_without_rows(deps: Deps) -> None:
    demo = deps.forms.current("demo")  # pede e-mail; não pede WhatsApp
    assert demo.screen is not None
    no_email = replace(
        demo,
        screen=replace(demo.screen, consent=replace(demo.screen.consent, marketing_email=None)),
    )

    with pytest.raises(ValidationError) as whatsapp:
        submit(deps, **marketing(marketing_whatsapp=True))
    with pytest.raises(ValidationError) as email:
        submit(deps, form=no_email, **marketing(marketing_email=True))

    (error,) = whatsapp.value.errors(include_input=False, include_url=False)
    assert error["loc"] == ("consent", "marketing_whatsapp")
    assert error["msg"] == "Este formulário não solicita autorização de marketing por este canal."
    assert [e["loc"] for e in email.value.errors()] == [("consent", "marketing_email")]
    assert count(deps, Lead) == 0 and count(deps, Submission) == 0 and count(deps, Job) == 0


def test_form_without_a_screen_does_not_declare_channels_and_keeps_the_choice(deps: Deps) -> None:
    code_form = replace(deps.forms.current("demo"), screen=None)  # o antigo, só em código

    submit(deps, form=code_form, **marketing(marketing_email=True, marketing_whatsapp=True))

    with deps.sessions() as session:
        consent = session.scalars(sa.select(Submission)).one().consent
    assert consent["marketing_email"] is True and consent["marketing_whatsapp"] is True


def test_replay_with_same_key_returns_same_job_and_token(deps: Deps) -> None:
    first = submit(deps, key="mesma")
    again = submit(deps, key="mesma")
    assert again.replay
    assert again.job.id == first.job.id and again.token == first.token
    assert count(deps, Job) == 1


def test_same_key_with_different_answers_is_a_conflict(deps: Deps) -> None:
    submit(deps, key="mesma")
    with pytest.raises(IdempotencyConflict):
        submit(deps, key="mesma", answers={**DEMO_ANSWERS, "principal_desafio": "vendas"})


def test_lead_is_deduplicated_by_email_without_changing_the_profile(deps: Deps) -> None:
    submit(deps, key="a")
    submit(
        deps,
        key="b",
        answers={**DEMO_ANSWERS, "email": "CONTADORA@example.test", "nome": "Nome Novo"},
    )
    with deps.sessions() as session:
        leads = session.scalars(sa.select(Lead)).all()
    assert len(leads) == 1 and leads[0].name == DEMO_ANSWERS["nome"]
    assert count(deps, Job) == 2


# --- limites de envio ---------------------------------------------------------------------------

FIRST_SUBMISSION = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


def send_three_from_the_same_email(deps: Deps) -> None:
    """Três envios da mesma lead, escritos de três jeitos (o e-mail é normalizado)."""
    for number, email in enumerate(
        (" Contadora@Example.test ", "contadora@example.test", "CONTADORA@example.test")
    ):
        submit(deps, key=f"k{number}", answers={**DEMO_ANSWERS, "email": email})


def stamp_submissions(deps: Deps, *moments: datetime) -> None:
    """Fixa o horário de criação dos envios, do mais antigo para o mais novo."""
    with deps.sessions.begin() as session:
        ids = session.scalars(sa.select(Submission.id).order_by(Submission.created_at)).all()
        for submission_id, moment in zip(ids, moments, strict=True):
            session.execute(
                sa.update(Submission)
                .where(Submission.id == submission_id)
                .values(created_at=moment)
            )


def test_fourth_submission_from_the_same_email_in_a_day_is_refused(deps: Deps) -> None:
    send_three_from_the_same_email(deps)

    with pytest.raises(SubmissionLimitExceeded) as refused:
        submit(deps, key="k3")

    assert "contadora" not in f"{refused.value} {refused.value!r}"  # nenhum dado da lead
    assert count(deps, Submission) == 3 and count(deps, Job) == 3


def test_the_refusal_says_how_long_until_the_oldest_submission_leaves_the_window(
    deps: Deps,
) -> None:
    send_three_from_the_same_email(deps)
    stamp_submissions(
        deps,
        FIRST_SUBMISSION,
        FIRST_SUBMISSION + timedelta(hours=1),
        FIRST_SUBMISSION + timedelta(hours=2),
    )
    three_hours_in = FIRST_SUBMISSION + timedelta(hours=3)

    with pytest.raises(SubmissionLimitExceeded) as refused:
        submit(deps, key="k3", now=three_hours_in)

    assert refused.value.retry_after == 21 * 3600  # o primeiro sai da janela às 12h do dia seguinte
    just_before = FIRST_SUBMISSION + DAY - timedelta(seconds=1)
    with pytest.raises(SubmissionLimitExceeded):
        submit(deps, key="k3", now=just_before)
    assert not submit(deps, key="k3", now=FIRST_SUBMISSION + DAY).replay


def test_the_limit_counts_each_email_and_each_form_apart(deps: Deps) -> None:
    send_three_from_the_same_email(deps)
    another_form = replace(deps.forms.current("demo"), slug="demo-copia")

    assert not submit(deps, key="outro-email", answers=lead_answers(1)).replay
    assert not submit(deps, key="outro-formulario", form=another_form).replay


def test_replaying_a_counted_submission_is_not_refused_by_the_limit(deps: Deps) -> None:
    first = submit(deps, key="k0")
    submit(deps, key="k1")
    submit(deps, key="k2")

    again = submit(deps, key="k0")

    assert again.replay and again.job.id == first.job.id and again.token == first.token
    assert count(deps, Submission) == 3
    with pytest.raises(SubmissionLimitExceeded):
        submit(deps, key="k3")  # o replay não abriu vaga nem gastou outra


def test_submissions_are_refused_while_the_queue_is_full(deps: Deps) -> None:
    limits = replace(deps.submission_limits, queue_max=2)
    first = submit(deps, key="a", answers=lead_answers(1), limits=limits)
    second = submit(deps, key="b", answers=lead_answers(2), limits=limits)

    with pytest.raises(QueueFull) as full:
        submit(deps, key="c", answers=lead_answers(3), limits=limits)

    assert full.value.retry_after == 300
    assert count(deps, Job) == 2 and count(deps, Submission) == 2
    assert count(deps, Lead) == 2  # a lead recusada também não foi gravada
    again = submit(deps, key="a", answers=lead_answers(1), limits=limits)
    assert again.replay and again.job.id == first.job.id  # replay não cria pedido: passa

    with deps.sessions.begin() as session:
        session.execute(sa.update(Job).where(Job.id == first.job.id).values(status="completed"))
        session.execute(sa.update(Job).where(Job.id == second.job.id).values(status="running"))
    assert not submit(deps, key="c", answers=lead_answers(3), limits=limits).replay  # 1 em curso
    with pytest.raises(QueueFull):  # running e queued contam: já são 2 de novo
        submit(deps, key="d", answers=lead_answers(4), limits=limits)


def test_jobs_that_already_finished_do_not_fill_the_queue(deps: Deps) -> None:
    limits = replace(deps.submission_limits, queue_max=1)
    first = submit(deps, key="a", answers=lead_answers(1), limits=limits)
    with deps.sessions.begin() as session:
        session.execute(sa.update(Job).where(Job.id == first.job.id).values(status="failed"))

    assert not submit(deps, key="b", answers=lead_answers(2), limits=limits).replay


def test_identical_requests_at_the_same_time_replay_instead_of_hitting_the_limit(
    deps: Deps,
) -> None:
    """A segunda espera a primeira terminar (a linha da lead trava) e, ao contar, já a encontra: o
    resultado tem de ser o replay, não o 429."""
    limits = replace(deps.submission_limits, per_email=1)
    replays: list[Accepted] = []
    failures: list[BaseException] = []

    def second_request() -> None:
        try:
            replays.append(submit(deps, key="mesma", limits=limits))
        except BaseException as exc:  # noqa: BLE001 - o teste confere o que veio
            failures.append(exc)

    body = SubmissionIn.model_validate({"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT})
    with deps.sessions() as first_session:
        first = accept_submission(
            first_session,
            deps.forms.current("demo"),
            body,
            idempotency_key="mesma",
            signer=deps.signer,
            link_ttl=deps.link_ttl,
            limits=limits,
        )
        thread = threading.Thread(target=second_request)
        thread.start()
        time.sleep(0.5)  # a segunda pede a linha da lead, que a primeira ainda segura
        assert thread.is_alive()
        first_session.commit()
    thread.join(10)

    assert failures == []
    assert replays[0].replay and replays[0].job.id == first.job.id
    assert count(deps, Submission) == 1
