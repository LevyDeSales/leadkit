"""As imagens dos formulários: enviar, trocar, remover, o que cada versão fixa e o que o rascunho
enxerga. Storage local de teste; as imagens são geradas aqui."""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from PIL import Image

from app.adapters.assets import AssetStore, AssetUnavailable
from app.deps import Deps
from app.errors import FailureCode, RetryableError
from app.forms import assets
from app.forms.assets import (
    AssetChanged,
    AssetInUse,
    AssetRefused,
    MissingImages,
    UnknownAsset,
)
from app.forms.definition.images import AssetRef
from app.models import AuditLog, Form, FormAsset, FormVersion, FormVersionAsset

ACTOR = "operadora"


def picture(
    color: tuple[int, int, int] = (200, 30, 60), form: str = "PNG", size=(300, 200)
) -> bytes:
    out = io.BytesIO()
    image = Image.new("RGB", size, color)
    image.paste(Image.new("RGB", (size[0] // 2, size[1] // 2), (255, 255, 255)), (10, 10))
    image.save(out, form)
    return out.getvalue()


def send(deps: Deps, name: str = "logo-black", scope: str = "", content: bytes | None = None):
    return assets.upload(
        deps,
        scope=scope,
        name=name,
        content=content if content is not None else picture(),
        actor=ACTOR,
    )


def stored(deps: Deps, ref: AssetRef) -> bytes:
    return AssetStore(deps.storage).read(ref)


def has_file(deps: Deps, ref: AssetRef) -> bool:
    try:
        stored(deps, ref)
    except AssetUnavailable:
        return False
    return True


def audit(deps: Deps, action: str) -> list[AuditLog]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(AuditLog).where(AuditLog.action == action)))


def pin(deps: Deps, asset: assets.Asset, slug: str = "demo", number: int = 1, name="logo") -> None:
    """Uma versão publicada que usa a imagem, como a publicação a fixa."""
    with deps.sessions.begin() as session:
        version_id = session.scalar(
            sa.select(FormVersion.id)
            .join(Form, Form.id == FormVersion.form_id)
            .where(Form.slug == slug, FormVersion.number == number)
        )
        session.add(
            FormVersionAsset(
                form_version_id=version_id,
                name=name,
                asset_id=asset.id,
                sha256=asset.ref.sha256,
                content_type=asset.ref.content_type,
            )
        )


# --- enviar --------------------------------------------------------------------------------------


def test_an_upload_stores_the_optimized_bytes_by_content_and_the_metadata(deps: Deps) -> None:
    content = picture(size=(3000, 1000))

    done = send(deps, content=content)

    asset = done.asset
    assert asset.library and asset.scope == "Biblioteca geral"
    assert (asset.width, asset.height) == (2000, 667)
    assert asset.source_size == len(content) and asset.byte_size == len(stored(deps, asset.ref))
    assert asset.ref.key == f"assets/{asset.ref.sha256}.{asset.ref.extension}"
    assert asset.created_by == ACTOR == asset.updated_by
    with deps.sessions() as session:
        row = session.get_one(FormAsset, asset.id)
    assert row.storage_key == asset.ref.key and row.form_id is None


def test_the_upload_says_what_the_optimization_did(deps: Deps) -> None:
    done = send(deps, content=picture(size=(3000, 1000)))

    assert done.image.resized and done.image.source_width == 3000
    assert done.image.size == done.asset.byte_size


def test_the_optimization_is_the_numbers_of_the_file_sent_and_the_file_stored(
    deps: Deps,
) -> None:
    sent_content = picture(size=(3000, 1000))

    done = send(deps, content=sent_content)

    before, after = done.optimization.before, done.optimization.after
    assert (before.size, before.content_type) == (len(sent_content), "image/png")
    assert (before.width, before.height) == (3000, 1000)
    assert (after.size, after.content_type) == (done.asset.byte_size, done.asset.ref.content_type)
    assert (after.width, after.height) == (done.asset.width, done.asset.height) == (2000, 667)
    assert done.optimization.saved_percent == round((before.size - after.size) * 100 / before.size)


def test_a_file_that_did_not_shrink_saved_nothing() -> None:
    assert assets.saved_percent(1000, 1000) is None
    assert assets.saved_percent(1000, 1400) is None
    assert assets.saved_percent(1000, 250) == 75
    assert assets.saved_percent(3, 2) == 33  # arredonda, como a frase da tela sempre fez


def test_an_upload_goes_to_the_audit_log_without_the_bytes(deps: Deps) -> None:
    done = send(deps, scope="demo")

    (entry,) = audit(deps, "asset.upload")
    assert entry.actor == ACTOR and entry.target_id == str(done.asset.id)
    assert entry.details["name"] == "logo-black" and entry.details["form_slug"] == "demo"
    assert entry.details["sha256"] == done.asset.ref.sha256
    assert "content" not in entry.details


def test_an_image_can_belong_to_a_form(deps: Deps) -> None:
    done = send(deps, scope="mcc")

    assert done.asset.form_slug == "mcc" and not done.asset.library
    assert done.asset.scope == done.asset.form_title


def test_sending_the_same_image_under_two_names_keeps_one_file(deps: Deps) -> None:
    content = picture()

    one, two = send(deps, "um", content=content), send(deps, "dois", content=content)

    assert one.asset.ref == two.asset.ref
    assert len(list((deps.storage.root / "assets").iterdir())) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ["", "Logo", "logo preto", "-logo", "a/b"])
def test_a_bad_name_is_refused_and_nothing_is_stored(deps: Deps, name: str) -> None:
    with pytest.raises(AssetRefused) as refused:
        send(deps, name)

    assert refused.value.field == "name"
    assert not (deps.storage.root / "assets").exists()  # type: ignore[attr-defined]


def test_a_name_in_use_in_the_same_scope_is_refused(deps: Deps) -> None:
    send(deps, "logo-black")
    send(deps, "logo-black", scope="demo")

    for scope in ("", "demo"):
        with pytest.raises(AssetRefused, match="já existe") as refused:
            send(deps, "logo-black", scope=scope, content=picture((1, 2, 3)))
        assert refused.value.field == "name"


def test_the_same_name_can_live_in_the_library_and_in_each_form(deps: Deps) -> None:
    send(deps, "logo-black")
    send(deps, "logo-black", scope="demo")
    send(deps, "logo-black", scope="mcc")

    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FormAsset)) == 3


def test_an_unknown_form_is_refused(deps: Deps) -> None:
    with pytest.raises(AssetRefused) as refused:
        send(deps, scope="nao-existe")

    assert refused.value.field == "scope"


@pytest.mark.parametrize(
    "content",
    [b"", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"nao e imagem", picture(form="GIF")],
)
def test_a_file_that_is_not_an_accepted_image_is_refused_with_the_reason(
    deps: Deps, content: bytes
) -> None:
    with pytest.raises(AssetRefused) as refused:
        send(deps, content=content)

    assert refused.value.field == "file" and refused.value.message
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FormAsset)) == 0


def test_the_list_has_the_library_first_and_then_each_form_by_name(deps: Deps) -> None:
    send(deps, "zeta")
    send(deps, "alfa", scope="mcc", content=picture((1, 1, 1)))
    send(deps, "beta", scope="demo", content=picture((2, 2, 2)))
    send(deps, "alfa")

    with deps.sessions() as session:
        listed = [(item.form_slug, item.name) for item in assets.listing(session)]

    assert listed == [(None, "alfa"), (None, "zeta"), ("demo", "beta"), ("mcc", "alfa")]


def test_a_form_sees_the_library_and_its_own_images_and_its_own_wins(deps: Deps) -> None:
    library = send(deps, "logo-black", content=picture((1, 1, 1)))
    send(deps, "so-da-biblioteca", content=picture((2, 2, 2)))
    own = send(deps, "logo-black", scope="demo", content=picture((3, 3, 3)))
    send(deps, "so-do-mcc", scope="mcc", content=picture((4, 4, 4)))

    with deps.sessions() as session:
        demo = assets.visible(session, "demo")
        mcc = assets.visible(session, "mcc")

    assert sorted(demo) == ["logo-black", "so-da-biblioteca"]
    assert demo["logo-black"].ref == own.asset.ref
    assert sorted(mcc) == ["logo-black", "so-da-biblioteca", "so-do-mcc"]
    assert mcc["logo-black"].ref == library.asset.ref


# --- trocar --------------------------------------------------------------------------------------


def swap(deps: Deps, asset: assets.Asset, content: bytes, expected: str | None = None):
    return assets.replace(
        deps, asset.id, content, expected=expected or asset.ref.sha256, actor="outra-pessoa"
    )


def test_a_replacement_changes_the_content_and_keeps_the_name_and_the_id(deps: Deps) -> None:
    old = send(deps).asset

    done = swap(deps, old, picture((9, 9, 9)))

    assert done.changed and done.asset.id == old.id and done.asset.name == old.name
    assert done.asset.ref != old.ref
    assert done.asset.created_by == ACTOR and done.asset.updated_by == "outra-pessoa"
    assert stored(deps, done.asset.ref)


def test_a_replacement_deletes_the_old_file_when_nothing_else_points_to_it(deps: Deps) -> None:
    old = send(deps).asset

    swap(deps, old, picture((9, 9, 9)))

    assert not has_file(deps, old.ref)


def test_a_replacement_keeps_the_old_file_a_published_version_pinned(deps: Deps) -> None:
    old = send(deps).asset
    pin(deps, old)

    swap(deps, old, picture((9, 9, 9)))

    assert has_file(deps, old.ref)


def test_a_replacement_keeps_the_old_file_another_name_still_uses(deps: Deps) -> None:
    content = picture()
    old = send(deps, "um", content=content).asset
    send(deps, "dois", content=content)

    swap(deps, old, picture((9, 9, 9)))

    assert has_file(deps, old.ref)


def test_replacing_with_the_same_image_changes_nothing(deps: Deps) -> None:
    content = picture()
    old = send(deps, content=content).asset

    done = swap(deps, old, content)

    assert not done.changed and done.asset == old
    assert audit(deps, "asset.replace") == []


def test_a_replacement_for_a_page_opened_before_another_replacement_is_refused(deps: Deps) -> None:
    old = send(deps).asset
    swap(deps, old, picture((9, 9, 9)))

    with pytest.raises(AssetChanged):
        swap(deps, old, picture((7, 7, 7)))  # `old` ainda traz o sha256 de antes


def test_a_replacement_with_a_bad_file_leaves_the_image_as_it_was(deps: Deps) -> None:
    old = send(deps).asset

    with pytest.raises(AssetRefused):
        swap(deps, old, b"nao e imagem")

    with deps.sessions() as session:
        assert assets.find(session, old.id) == old
    assert has_file(deps, old.ref)


def test_a_replacement_goes_to_the_audit_log_with_both_contents(deps: Deps) -> None:
    old = send(deps).asset
    done = swap(deps, old, picture((9, 9, 9)))

    (entry,) = audit(deps, "asset.replace")
    assert entry.actor == "outra-pessoa"
    assert entry.details["sha256"] == done.asset.ref.sha256
    assert entry.details["previous_sha256"] == old.ref.sha256


def test_replacing_an_image_that_does_not_exist(deps: Deps) -> None:
    with pytest.raises(UnknownAsset):
        assets.replace(deps, uuid.uuid4(), picture(), expected="0" * 64, actor=ACTOR)


# --- remover -------------------------------------------------------------------------------------


def test_removing_an_unused_image_deletes_the_row_and_the_file(deps: Deps) -> None:
    asset = send(deps).asset

    gone = assets.remove(deps, asset.id, expected=asset.ref.sha256, actor=ACTOR)

    assert gone == asset
    with deps.sessions() as session:
        assert assets.find(session, asset.id) is None
    assert not has_file(deps, asset.ref)
    (entry,) = audit(deps, "asset.remove")
    assert entry.details["name"] == "logo-black"


def test_removing_an_image_a_published_version_uses_is_refused_and_says_which(
    deps: Deps,
) -> None:
    asset = send(deps).asset
    pin(deps, asset, "demo", 1)
    pin(deps, asset, "mcc", 1)

    with pytest.raises(AssetInUse) as refused:
        assets.remove(deps, asset.id, expected=asset.ref.sha256, actor=ACTOR)

    assert [(use.form_slug, use.number) for use in refused.value.uses] == [("demo", 1), ("mcc", 1)]
    with deps.sessions() as session:
        assert assets.find(session, asset.id) is not None
    assert has_file(deps, asset.ref) and audit(deps, "asset.remove") == []


def test_the_versions_that_use_several_images_come_in_one_reading_by_image(deps: Deps) -> None:
    both = send(deps, "usada").asset
    one = send(deps, "so-uma", content=picture((1, 2, 3))).asset
    idle = send(deps, "parada", content=picture((9, 9, 9))).asset
    pin(deps, both, "demo", 1, name="usada")
    pin(deps, both, "mcc", 1, name="usada")
    pin(deps, one, "demo", 1, name="so-uma")

    with deps.sessions() as session:
        found = assets.uses_of(session, [both.id, one.id, idle.id, uuid.uuid4()])
        alone = assets.uses(session, both.id)

    assert [(use.form_slug, use.number) for use in found[both.id]] == [("demo", 1), ("mcc", 1)]
    assert [(use.form_slug, use.number) for use in found[one.id]] == [("demo", 1)]
    assert idle.id not in found and len(found) == 2  # a imagem parada e a que não existe: sem linha
    assert alone == found[both.id]
    with deps.sessions() as session:
        assert assets.uses_of(session, []) == {}


def test_the_refusal_to_remove_an_image_in_use_says_which_versions_use_it() -> None:
    one = AssetInUse([assets.Use("demo", 2)])
    two = AssetInUse([assets.Use("demo", 2), assets.Use("mcc", 1)])

    assert one.message() == (
        "Esta imagem não pode ser removida: A versão publicada demo v2 usa esta imagem. Para "
        "mudá-la, troque o arquivo."
    )
    assert two.message() == (
        "Esta imagem não pode ser removida: As versões publicadas demo v2, mcc v1 usam esta "
        "imagem. Para mudá-la, troque o arquivo."
    )


def test_a_publication_that_slipped_in_gives_the_refusal_without_a_list() -> None:
    """O banco recusou a remoção porque uma publicação passou a usar a imagem no meio do caminho."""
    assert AssetInUse(()).message() == (
        "Esta imagem não pode ser removida: uma versão publicada passou a usá-la agora."
    )


def test_a_storage_failure_says_nothing_was_stored_and_names_the_cause_without_the_content() -> (
    None
):
    failed = RetryableError(FailureCode.STORAGE_FAILED, "OSError")

    assert assets.storage_failure(failed) == (
        "Não deu para guardar a imagem no storage (OSError). Nada foi gravado: tente de novo."
    )


def test_removing_keeps_the_file_when_another_image_has_the_same_content(deps: Deps) -> None:
    content = picture()
    first = send(deps, "um", content=content).asset
    second = send(deps, "dois", content=content).asset

    assets.remove(deps, first.id, expected=first.ref.sha256, actor=ACTOR)

    assert has_file(deps, second.ref)


def test_removing_a_page_opened_before_a_replacement_is_refused(deps: Deps) -> None:
    old = send(deps).asset
    swap(deps, old, picture((9, 9, 9)))

    with pytest.raises(AssetChanged):
        assets.remove(deps, old.id, expected=old.ref.sha256, actor=ACTOR)


def test_removing_an_image_that_does_not_exist(deps: Deps) -> None:
    with pytest.raises(UnknownAsset):
        assets.remove(deps, uuid.uuid4(), expected="0" * 64, actor=ACTOR)


def test_a_failing_storage_does_not_undo_the_removal(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = send(deps).asset

    def broken(self: AssetStore, ref: AssetRef) -> None:
        raise OSError("disco cheio")

    monkeypatch.setattr(AssetStore, "discard", broken)

    assets.remove(deps, asset.id, expected=asset.ref.sha256, actor=ACTOR)

    with deps.sessions() as session:
        assert assets.find(session, asset.id) is None


# --- o que os templates usam ---------------------------------------------------------------------

DOCUMENT: dict[str, Any] = {
    "slug": "demo",
    "pdf": {"html": '<img src="asset:logo-black"><img src="asset:capa">', "css": ""},
    "preview": '<img src="asset:logo-black">',
}


def test_the_names_a_definition_uses_come_from_the_pdf_the_css_and_the_preview() -> None:
    document = {
        "pdf": {"html": '<img src="asset:a">', "css": ".x { background: url(asset:b) }"},
        "preview": '<img src="asset:c">',
    }

    assert assets.used_names(document) == ["a", "b", "c"]
    assert assets.used_names({}) == [] and assets.used_names({"pdf": "x", "preview": 3}) == []


def test_the_draft_sees_the_images_of_now_and_ignores_names_that_do_not_exist(deps: Deps) -> None:
    logo = send(deps, "logo-black").asset

    with deps.sessions() as session:
        live = assets.live_refs(session, DOCUMENT)

    assert live == {"logo-black": logo.ref}


def test_publishing_pins_the_content_of_each_image_the_version_uses(deps: Deps) -> None:
    logo = send(deps, "logo-black").asset
    capa = send(deps, "capa", scope="demo", content=picture((5, 5, 5))).asset
    send(deps, "nao-usada", content=picture((6, 6, 6)))

    with deps.sessions.begin() as session:
        version_id = session.scalar(sa.select(FormVersion.id).limit(1))
        pinned = assets.pin_version(session, version_id, "demo", DOCUMENT)

    assert pinned == {"logo-black": logo.ref, "capa": capa.ref}
    with deps.sessions() as session:
        rows = session.scalars(sa.select(FormVersionAsset)).all()
    assert {(row.name, row.asset_id) for row in rows} == {
        ("logo-black", logo.id),
        ("capa", capa.id),
    }


def test_pinning_a_name_that_does_not_exist_stops_the_publication(deps: Deps) -> None:
    send(deps, "logo-black")

    with pytest.raises(MissingImages) as missing, deps.sessions.begin() as session:
        version_id = session.scalar(sa.select(FormVersion.id).limit(1))
        assets.pin_version(session, version_id, "demo", DOCUMENT)

    assert missing.value.names == ("capa",)
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FormVersionAsset)) == 0


def test_a_version_gives_back_the_content_it_pinned_after_the_image_is_replaced(
    deps: Deps,
) -> None:
    logo = send(deps, "logo-black").asset
    send(deps, "capa", content=picture((5, 5, 5)))
    with deps.sessions.begin() as session:
        version_id = session.scalar(
            sa.select(FormVersion.id)
            .join(Form, Form.id == FormVersion.form_id)
            .where(Form.slug == "demo")
        )
        assets.pin_version(session, version_id, "demo", DOCUMENT)

    swapped = swap(deps, logo, picture((9, 9, 9))).asset

    with deps.sessions() as session:
        pinned = assets.version_refs(session, "demo", 1)
        live = assets.live_refs(session, DOCUMENT)
    assert pinned["logo-black"] == logo.ref and live["logo-black"] == swapped.ref
    assert has_file(deps, logo.ref)


def test_a_replaced_image_makes_the_draft_that_uses_it_stale(deps: Deps) -> None:
    logo = send(deps, "logo-black").asset
    send(deps, "capa", content=picture((5, 5, 5)))
    with deps.sessions.begin() as session:
        version_id = session.scalar(
            sa.select(FormVersion.id)
            .join(Form, Form.id == FormVersion.form_id)
            .where(Form.slug == "demo")
        )
        assets.pin_version(session, version_id, "demo", DOCUMENT)
    with deps.sessions() as session:
        assert assets.stale_names(session, "demo", DOCUMENT, 1) == ()

    swap(deps, logo, picture((9, 9, 9)))

    with deps.sessions() as session:
        assert assets.stale_names(session, "demo", DOCUMENT, 1) == ("logo-black",)
        assert assets.stale_names(session, "demo", {"slug": "demo"}, 1) == ()
        assert assets.stale_names(session, "demo", DOCUMENT, None) == ()


def test_the_public_route_serves_what_an_image_or_a_version_has_and_nothing_else(
    deps: Deps,
) -> None:
    old = send(deps).asset
    pin(deps, old)
    new = swap(deps, old, picture((9, 9, 9))).asset

    with deps.sessions() as session:
        assert assets.servable(session, new.ref)
        assert assets.servable(session, old.ref)  # fixada por uma versão
        assert not assets.servable(session, AssetRef("0" * 64, "image/png"))
        assert not assets.servable(session, AssetRef(new.ref.sha256, "image/jpeg"))
