"""O perfil de captura conserva a referência real da submissão, sem dados PDF fictícios."""

from dataclasses import asdict

from conftest import legacy_spec

from app.integrations import mapping


def test_capture_catalog_and_sample_expose_submission_without_pdf_capabilities() -> None:
    spec = legacy_spec("demo")
    sources = mapping.catalog(spec, profile="capture")
    assert {
        "submission.id",
        "submission.form_version",
        "submission.answers_json",
        "submission.consent_json",
        "submission.tracking_json",
        "submission.scores_json",
        "submission.priority",
        "submission.persona",
        "lead.email",
        "consent.privacy",
        "answer:tamanho_equipe",
    } <= sources.keys()
    assert not any(key.startswith("job.") for key in sources)
    data = mapping.sample_data(spec, "synthetic-submission", profile="capture")
    assert isinstance(data, mapping.CaptureRowData)
    assert data.submission_id == "synthetic-submission"
    assert not {"public_id", "pdf_url", "page_url"} & asdict(data).keys()
    assert mapping.raw_value("submission.id", data, profile="capture") == "synthetic-submission"
    assert "job.pdf_url" in mapping.catalog(spec)


def test_capture_mapping_requires_its_own_key_and_rejects_pdf_sources() -> None:
    import pytest
    from test_integration_mapping import live_fields

    spec = legacy_spec("demo")
    sources = mapping.catalog(spec, profile="capture")
    entries = [mapping.Entry(104, "Código", "text", "submission.id")]
    mapping.check_entries(entries, live_fields(), sources, profile="capture")
    assert mapping.key_entry(entries, profile="capture") == entries[0]
    assert mapping.key_entry(entries) is None
    assert mapping.build_report(entries, live_fields(), spec, profile="capture").ok
    for invalid in ([], [mapping.Entry(104, "Código", "text", "job.public_id")], entries * 2):
        with pytest.raises(mapping.InvalidMapping):
            mapping.check_entries(invalid, live_fields(), sources, profile="capture")
        assert not mapping.build_report(invalid, live_fields(), spec, profile="capture").ok
    with pytest.raises(mapping.InvalidMapping):
        mapping.check_entries(
            entries + [mapping.Entry(106, "PDF", "url", "job.pdf_url")],
            live_fields(),
            mapping.catalog(spec),
            profile="capture",
        )


def test_capture_row_reuses_options_and_preserves_legitimate_missing_answer_semantics() -> None:
    from dataclasses import replace

    from test_integration_mapping import live_fields

    data = mapping.sample_data(legacy_spec("demo"), "submission-1", profile="capture")
    data = replace(data, answers={"tamanho_equipe": "2_a_5"})
    entries = [
        mapping.Entry(104, "Código", "text", "submission.id"),
        mapping.Entry(108, "Equipe", "single_select", "answer:tamanho_equipe"),
        mapping.Entry(109, "Desafio", "single_select", "answer:principal_desafio"),
        mapping.Entry(107, "Consentiu", "boolean", "consent.privacy"),
    ]
    assert mapping.build_row(entries, live_fields(), data, profile="capture") == {
        "field_104": "submission-1",
        "field_108": 10802,
        "field_109": None,
        "field_107": True,
    }


def test_unavailable_sources_are_refused_before_missing_values_can_clear_remote_fields() -> None:
    import pytest
    from test_integration_mapping import live_fields

    for profile in ("pdf", "capture"):
        data = mapping.sample_data(legacy_spec("demo"), "id-1", profile=profile)
        invalid = ["answer:removed_question", "unknown", "submission.pdf_url"]
        invalid.append("job.pdf_url" if profile == "capture" else "submission.id")
        for source in invalid:
            with pytest.raises(mapping.MappingError):
                mapping.raw_value(source, data, profile=profile)
            with pytest.raises(mapping.MappingError):
                mapping.build_row(
                    [mapping.Entry(104, "Código", "text", source)],
                    live_fields(),
                    data,
                    profile=profile,
                )


def test_capture_validation_and_report_refuse_a_field_that_would_overwrite_the_record_key() -> None:
    import pytest
    from test_integration_mapping import live_fields

    spec = legacy_spec("demo")
    entries = [
        mapping.Entry(104, "Código", "text", "submission.id"),
        mapping.Entry(104, "Código", "text", "lead.name"),
    ]
    with pytest.raises(mapping.InvalidMapping):
        mapping.check_entries(
            entries, live_fields(), mapping.catalog(spec, profile="capture"), profile="capture"
        )
    assert not mapping.build_report(entries, live_fields(), spec, profile="capture").ok


def test_real_capture_spec_materializes_historical_submission_metadata() -> None:
    import json

    from app.forms.definition import compile_definition
    from app.forms.starter import definition
    from app.integrations.baserow import BaserowField

    spec = compile_definition(
        definition(slug="capture-mapping", title="Pesquisa sintética", generation="none"),
        version_number=3,
    )
    data = mapping.CaptureRowData(
        submission_id="00000000-0000-0000-0000-000000000123",
        form_version=spec.version,
        lead_name="Participante Sintética",
        lead_email="participant@example.test",
        lead_phone=None,
        priority="alta",
        persona="iniciante",
        answers={"objetivo": "Resposta histórica"},
        consent={"processing": True},
        tracking={"utm_source": "sintetica"},
        scores={"total": 7},
        questions={question.name: question for question in mapping.questions_of(spec)},
    )
    sources = mapping.catalog(spec, profile="capture")
    selected = [source for source in sources.values() if source.key.startswith("submission.")]
    entries = [
        mapping.Entry(i, source.label, "text", source.key) for i, source in enumerate(selected, 1)
    ]
    live = [BaserowField(entry.field_id, entry.name, entry.type) for entry in entries]
    mapping.check_entries(entries, live, sources, profile="capture")
    assert mapping.build_report(entries, live, spec, profile="capture").ok
    row = mapping.build_row(entries, live, data, profile="capture")
    values = {entry.source: row[f"field_{entry.field_id}"] for entry in entries}
    assert values["submission.id"] == "00000000-0000-0000-0000-000000000123"
    assert values["submission.form_version"] == spec.version
    assert values["submission.priority"] == "alta"
    assert values["submission.persona"] == "iniciante"
    assert json.loads(values["submission.answers_json"]) == {"objetivo": "Resposta histórica"}
    assert json.loads(values["submission.consent_json"]) == {"processing": True}
    assert json.loads(values["submission.tracking_json"]) == {"utm_source": "sintetica"}
    assert json.loads(values["submission.scores_json"]) == {"total": 7}


def test_capture_report_refuses_read_only_key_before_activation() -> None:
    from app.integrations.baserow import BaserowField

    spec = legacy_spec("demo")
    entries = [mapping.Entry(104, "Código", "text", "submission.id")]
    report = mapping.build_report(
        entries, [BaserowField(104, "Código", "text", read_only=True)], spec, profile="capture"
    )
    assert not report.ok
    assert any("somente leitura" in problem for problem in report.problems)
