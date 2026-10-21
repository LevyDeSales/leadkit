"""O guia público acompanha a versão instalada, com fallback seguro para builds sem release."""

import re

import pytest

from app import version


def test_release_guide_uses_the_installed_tag() -> None:
    assert version.operator_guide_url("2026.10.1") == (
        "https://github.com/LevyDeSales/leadkit/blob/v2026.10.1/docs/guia-da-operadora/README.md"
    )


@pytest.mark.parametrize(
    "number",
    [
        None,
        "",
        "DEV",
        "dev",
        "a1b2c3d",
        "2026.13.1",
        "2026.10.01",
        "v2026.10.1",
        "../../main",
        '2026.10.1?x="<script>',
        "2026.10.1\n",
    ],
)
def test_non_release_builds_use_develop_without_interpolating_input(number: str | None) -> None:
    assert version.operator_guide_url(number) == (
        "https://github.com/LevyDeSales/leadkit/blob/develop/docs/guia-da-operadora/README.md"
    )


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (
            "2026.10.1",
            "https://github.com/LevyDeSales/leadkit/blob/v2026.10.1/docs/guia-da-operadora/README.md",
        ),
        (
            None,
            "https://github.com/LevyDeSales/leadkit/blob/develop/docs/guia-da-operadora/README.md",
        ),
    ],
)
def test_admin_navigation_renders_the_guide_for_the_build(
    number: str | None, expected: str
) -> None:
    from app.admin.routes import templates

    html = templates.env.get_template("admin/layout.html").render(
        session={"csrf_token": "test-csrf", "username": "operadora"},
        section="forms",
        env_label=None,
        flash=None,
        version_label=version.describe(number, None),
        operator_guide_url=version.operator_guide_url(number),
    )
    link = re.search(r'<a href="([^"]+)" title="([^"]+)">Guia de uso(.*?)</a>', html)
    assert link is not None
    assert link.group(1) == expected
    assert "no GitHub" in link.group(3)
    assert "desenvolvimento" not in link.group(2) + link.group(3)
