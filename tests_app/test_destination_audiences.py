"""Finalidade explícita dos receptores, sem promoção silenciosa a uso interno."""

import pytest

from app import destinations


def save(deps, *, purpose=None, expected_version=0, url="https://example.test/hook"):
    with deps.sessions.begin() as session:
        return destinations.save(
            session,
            deps.cipher,
            form_slug=None,
            url=url,
            actor="operadora",
            expected_version=expected_version,
            production=False,
            http_confirmed=False,
            purpose=purpose,
        )


def test_internal_requires_explicit_choice_and_rotation_preserves_it(deps):
    created = save(deps, purpose="internal")
    assert created.destination.purpose == "internal"
    assert created.destination.purpose_origin == "explicit"
    with deps.sessions.begin() as session:
        destinations.rotate(
            session, deps.cipher, form_slug=None, actor="operadora", expected_version=1
        )
    with deps.sessions() as session:
        stored = destinations.listing(session)[None]
    assert (stored.purpose, stored.purpose_origin) == ("internal", "explicit")


def test_changed_url_requires_an_explicit_purpose(deps):
    save(deps, purpose="internal")
    with pytest.raises(destinations.InvalidPurpose, match="finalidade"):
        save(deps, expected_version=1, url="https://example.test/other")
    unchanged = save(deps, expected_version=1)
    assert unchanged.destination.purpose == "internal"


def test_capture_rejects_participant_in_the_shared_service(deps):
    with (
        deps.sessions.begin() as session,
        pytest.raises(destinations.InvalidPurpose, match="captura"),
    ):
        destinations.save(
            session,
            deps.cipher,
            form_slug=None,
            url="https://example.test/capture",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="participant",
            events=["submissao.persistida"],
        )


def test_baserow_never_accepts_participant_or_an_implicit_internal_choice(deps):
    from app.integrations import config

    for purpose in (None, "participant"):
        with (
            deps.sessions.begin() as session,
            pytest.raises(destinations.InvalidPurpose, match="Baserow"),
        ):
            config.connect(
                session,
                deps.cipher,
                form_slug="demo",
                base_url="https://example.test",
                table_id=1,
                token="synthetic",
                actor="operadora",
                expected_version=0,
                production=False,
                purpose=purpose,
            )
