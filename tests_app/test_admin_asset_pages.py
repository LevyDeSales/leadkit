"""As frases das telas das imagens (`app.admin.asset_pages`) que vêm de números do serviço: o que a
otimização fez ao arquivo enviado. A API devolve os mesmos números (`app.forms.assets.Optimization`)
como dados; a página os escreve em português."""

from __future__ import annotations

from app.admin import asset_pages
from app.forms.assets import Measure, Optimization


def optimization(before: Measure, after: Measure) -> Optimization:
    return Optimization(before=before, after=after)


def test_the_sentence_says_the_size_the_type_and_the_dimensions_before_and_after() -> None:
    change = optimization(
        Measure(size=2_621_440, content_type="image/png", width=3000, height=1000),
        Measure(size=524_288, content_type="image/webp", width=2000, height=667),
    )

    assert asset_pages.optimization(change) == (
        "Antes: 2,5 MB (PNG, 3000 × 1000). Depois: 512 KB (WebP, 2000 × 667), 80% menor."
    )


def test_a_file_that_did_not_shrink_says_there_was_no_gain() -> None:
    change = optimization(
        Measure(size=40_960, content_type="image/jpeg", width=800, height=400),
        Measure(size=40_960, content_type="image/jpeg", width=800, height=400),
    )

    assert asset_pages.optimization(change).endswith(", sem ganho de tamanho.")
