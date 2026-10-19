"""As imagens do formulário na importação, no rascunho e na publicação: o nome que não existe é
recusado, a versão fixa o conteúdo de cada imagem e a troca de uma imagem pede uma versão nova.
O PDF é o do WeasyPrint de verdade, lendo as imagens do storage de teste."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
import sqlalchemy as sa
from PIL import Image
from pypdf import PdfReader
from test_admin_forms import DEFINITION, SLUG

from app.adapters.assets import AssetStore
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.deps import Deps
from app.forms import assets, draft_previews, draft_tests, drafts, stored
from app.forms.definition import DefinitionError
from app.models import AuditLog, FormVersionAsset

ACTOR = "operadora"
NOW = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
PDF_HTML = '<h1>{{ plan.titulo }}</h1><img src="asset:logo-black" alt="">'
PREVIEW = '<p>{{ plan.titulo }}</p><img src="asset:logo-black" alt="">'


def picture(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    image = Image.new("RGB", (240, 120), color)
    image.paste(Image.new("RGB", (60, 60), (255, 255, 255)), (20, 20))
    image.save(out, "PNG")
    return out.getvalue()


RED, BLUE = picture((220, 20, 40)), picture((20, 40, 220))


@pytest.fixture(autouse=True)
def real_pdf(deps: Deps) -> None:
    """O PDF de verdade, que abre as imagens do mesmo storage do teste."""
    deps.pdf = WeasyPrintRenderer(AssetStore(deps.storage))


def definition_with_images(**changes: Any) -> str:
    document = json.loads(DEFINITION)
    document["pdf"] = {**document["pdf"], "html": PDF_HTML}
    document["preview"] = PREVIEW
    document.update(changes)
    return json.dumps(document)


def upload(deps: Deps, content: bytes, name: str = "logo-black", scope: str = "") -> assets.Asset:
    return assets.upload(deps, scope=scope, name=name, content=content, actor=ACTOR).asset


def import_it(deps: Deps) -> stored.Imported:
    return stored.import_definition(deps, definition_with_images(), actor=ACTOR)


def images_in(pdf: bytes) -> int:
    return sum(len(page.images) for page in PdfReader(BytesIO(pdf)).pages)


def pins_of(deps: Deps, number: int) -> dict[str, str]:
    with deps.sessions() as session:
        return {
            name: ref.sha256 for name, ref in assets.version_refs(session, SLUG, number).items()
        }


def token(deps: Deps) -> str:
    with deps.sessions() as session:
        draft = drafts.load(session, SLUG)
    assert draft is not None
    return draft.token


def load(deps: Deps) -> drafts.Draft:
    with deps.sessions() as session:
        draft = drafts.load(session, SLUG)
    assert draft is not None
    return draft


def save_pdf(deps: Deps, html: str = PDF_HTML, preview: str = PREVIEW) -> drafts.Draft:
    texts = drafts.template_texts_of(load(deps).document)
    new = drafts.TemplateTexts(html, texts.pdf_css, preview, texts.scores)
    return drafts.save_templates(deps, SLUG, new, actor=ACTOR, expected=token(deps))


def publish(deps: Deps, note: str = "Imagem nova") -> drafts.Published:
    return drafts.publish(deps, SLUG, actor=ACTOR, note=note, expected=token(deps))


def pdf_of_version(deps: Deps, number: int) -> bytes:
    spec = deps.forms.get(SLUG, number)
    context = {
        "lead": {"name": "Contadora Exemplo"},
        "form": {"slug": SLUG, "title": spec.title},
        "scores": {},
        "generated_at": NOW,
    }
    return deps.pdf.render(spec, spec.example_plan, context).content


# --- importar ------------------------------------------------------------------------------------


def test_importing_a_definition_that_asks_for_an_image_that_does_not_exist_is_refused(
    deps: Deps,
) -> None:
    with pytest.raises(DefinitionError) as refused:
        import_it(deps)

    paths = {
        (problem.path, problem.message.split("«")[1].split("»")[0])
        for problem in refused.value.problems
    }
    assert ("pdf.html", "logo-black") in paths and ("preview", "logo-black") in paths


def test_importing_pins_the_content_of_the_image_to_version_one(deps: Deps) -> None:
    logo = upload(deps, RED)

    import_it(deps)

    assert pins_of(deps, 1) == {"logo-black": logo.ref.sha256}
    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.import"))
        assert session.scalar(sa.select(sa.func.count()).select_from(FormVersionAsset)) == 1
    assert entry.details["form_slug"] == SLUG


def test_the_pdf_of_an_imported_version_shows_its_image(deps: Deps) -> None:
    upload(deps, RED)
    import_it(deps)

    assert images_in(pdf_of_version(deps, 1)) == 1


def test_the_version_one_pdf_still_gets_its_image_after_the_registry_restarts(deps: Deps) -> None:
    upload(deps, RED)
    import_it(deps)
    deps.forms = stored.StoredRegistry(deps.sessions)  # um processo novo, sem cache

    assert deps.forms.get(SLUG, 1).templates.assets["logo-black"].sha256


def test_an_image_removed_between_the_check_and_the_import_stops_it_whole(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    logo = upload(deps, RED)
    real = stored.publication_problems

    def then_remove(*args: Any, **kwargs: Any) -> Any:
        found = real(*args, **kwargs)
        assets.remove(deps, logo.id, expected=logo.ref.sha256, actor=ACTOR)
        return found

    monkeypatch.setattr(stored, "publication_problems", then_remove)

    with pytest.raises(DefinitionError):
        import_it(deps)

    with deps.sessions() as session:
        assert not stored.listing(session, SLUG)


# --- o editor do PDF -----------------------------------------------------------------------------


def test_saving_the_pdf_with_a_missing_image_names_it_and_the_line(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)  # sem imagens

    with pytest.raises(DefinitionError) as refused:
        save_pdf(deps, "<h1>x</h1>\n<img src='asset:nao-existe'>", "<p>sem imagem</p>")

    (problem,) = refused.value.problems
    assert (problem.path, problem.line) == ("pdf.html", 2) and "nao-existe" in problem.message


def test_saving_the_pdf_with_an_image_that_exists_works(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    upload(deps, RED)

    saved = save_pdf(deps)

    assert saved.pending and "asset:logo-black" in saved.document["pdf"]["html"]


def test_an_image_of_another_form_is_not_visible_here(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    upload(deps, RED, scope="mcc")

    with pytest.raises(DefinitionError):
        save_pdf(deps)


def test_the_form_own_image_wins_over_the_library_one_with_the_same_name(deps: Deps) -> None:
    library = upload(deps, RED)
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    own = upload(deps, BLUE, scope=SLUG)
    save_pdf(deps)

    publish(deps)

    assert pins_of(deps, 2) == {"logo-black": own.ref.sha256} and own.ref != library.ref


# --- a prévia e o teste do rascunho --------------------------------------------------------------


def test_the_pdf_preview_of_the_draft_uses_the_image_of_now(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    logo = upload(deps, RED)
    draft = save_pdf(deps)
    texts = drafts.template_texts_of(draft.document)

    def preview() -> draft_previews.TemplatePreview:
        with deps.sessions() as session:
            library = {n: a.ref for n, a in assets.visible(session, SLUG).items()}
        return draft_previews.preview_of(
            load(deps), texts, source="example", tests=[], now=NOW, assets=library
        )

    before = preview()
    assets.replace(deps, logo.id, BLUE, expected=logo.ref.sha256, actor=ACTOR)
    after = preview()

    assert before.spec.templates.assets["logo-black"] == logo.ref
    assert after.spec.templates.assets["logo-black"] != logo.ref
    rendered = deps.pdf.render(after.spec, after.plan, after.context)
    assert images_in(rendered.content) == 1


def test_the_draft_test_renders_the_pdf_with_the_image_of_now(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    upload(deps, RED)
    save_pdf(deps)

    test_id = draft_tests.start(deps, SLUG, actor=ACTOR, now=NOW)
    assert draft_tests.run(deps, test_id, now=lambda: NOW) == "succeeded"

    with deps.sessions() as session:
        pdf = draft_tests.pdf_of(session, load(deps), test_id)
    assert pdf is not None and images_in(pdf) == 1


# --- publicar ------------------------------------------------------------------------------------


def test_publishing_pins_the_images_and_the_audit_log_names_their_content(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    logo = upload(deps, RED)
    save_pdf(deps)

    published = publish(deps)

    assert published.number == 2 and pins_of(deps, 2) == {"logo-black": logo.ref.sha256}
    assert pins_of(deps, 1) == {}
    with deps.sessions() as session:
        entry = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.publish")).one()
    assert entry.details["images"] == {"logo-black": logo.ref.sha256}


def test_a_replacement_does_not_change_the_pdf_of_the_published_version(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    logo = upload(deps, RED)
    save_pdf(deps)
    publish(deps)
    before = pdf_of_version(deps, 2)

    assets.replace(deps, logo.id, BLUE, expected=logo.ref.sha256, actor=ACTOR)
    deps.forms = stored.StoredRegistry(deps.sessions)  # sem o cache de versões compiladas

    assert pdf_of_version(deps, 2) == before


def test_a_replaced_image_is_something_to_publish_and_the_new_version_takes_it(
    deps: Deps,
) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    logo = upload(deps, RED)
    save_pdf(deps)
    publish(deps)
    assert not load(deps).pending

    swapped = assets.replace(deps, logo.id, BLUE, expected=logo.ref.sha256, actor=ACTOR).asset

    draft = load(deps)
    assert draft.stale_assets == ("logo-black",) and draft.pending and not draft.saved
    with deps.sessions() as session:
        assert SLUG in drafts.pending_slugs(session)
        found = drafts.review(session, draft)
    assert not found.nothing and found.llm_changes == ()  # sem teste com o LLM: o texto é o mesmo
    published = publish(deps, "Logo novo")
    assert published.number == 3
    assert pins_of(deps, 3) == {"logo-black": swapped.ref.sha256}
    assert pins_of(deps, 2) == {"logo-black": logo.ref.sha256}
    assert not load(deps).pending
    with deps.sessions() as session:
        assert SLUG not in drafts.pending_slugs(session)


def test_a_form_that_does_not_use_the_replaced_image_stays_as_it_was(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    logo = upload(deps, RED)

    assets.replace(deps, logo.id, BLUE, expected=logo.ref.sha256, actor=ACTOR)

    assert not load(deps).pending


def test_an_image_removed_between_the_review_and_the_publication_stops_it(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    logo = upload(deps, RED)
    save_pdf(deps)
    real = drafts.publication_problems

    def then_remove(*args: Any, **kwargs: Any) -> Any:
        found = real(*args, **kwargs)
        assets.remove(deps, logo.id, expected=logo.ref.sha256, actor=ACTOR)
        return found

    monkeypatch.setattr(drafts, "publication_problems", then_remove)

    with pytest.raises(drafts.PublishRefused) as refused:
        publish(deps)

    assert refused.value.reasons == (drafts.IMAGE_GONE,)
    assert load(deps).current.number == 1  # type: ignore[union-attr]
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FormVersionAsset)) == 0


def test_publishing_with_an_image_that_was_removed_is_refused_with_the_problem(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    logo = upload(deps, RED)
    save_pdf(deps)
    assets.remove(deps, logo.id, expected=logo.ref.sha256, actor=ACTOR)

    with pytest.raises(drafts.PublishRefused) as refused:
        publish(deps)

    assert any("logo-black" in problem.message for problem in refused.value.problems)


def test_an_image_a_published_version_uses_cannot_be_removed(deps: Deps) -> None:
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    logo = upload(deps, RED)
    save_pdf(deps)
    publish(deps)

    with pytest.raises(assets.AssetInUse) as refused:
        assets.remove(deps, logo.id, expected=logo.ref.sha256, actor=ACTOR)

    assert [(use.form_slug, use.number) for use in refused.value.uses] == [(SLUG, 2)]
