"""Os avisos acompanham os arquivos incorporados sem presumir licença MIT para terceiros."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_font_notice_identifies_every_original_file_and_its_license() -> None:
    folder = ROOT / "app/assets/fonts"
    notice = (folder / "NOTICE.txt").read_text()
    records = re.findall(r"(Source[\w-]+\.ttf) sha256 ([0-9a-f]{64})", notice)
    assert len(records) == len(list(folder.glob("*.ttf"))) == 8
    for name, digest in records:
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
    for name in ("LICENSE-SourceSansPro.txt", "LICENSE-SourceSerifPro.txt"):
        assert name in notice
        assert "SIL OPEN FONT LICENSE Version 1.1" in (folder / name).read_text()
    assert "Reserved Font Name" in notice and "reports/" not in notice


def test_htmx_keeps_the_license_of_the_exact_vendored_version() -> None:
    folder = ROOT / "app/web/static"
    license_file = folder / "vendor/htmx-2.0.11.LICENSE.txt"
    assert hashlib.sha256(license_file.read_bytes()).hexdigest() == (
        "d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38"
    )
    notice = (folder / "NOTICE.txt").read_text()
    assert "0BSD" in notice and "v2.0.11/LICENSE" in notice
    assert license_file.name in notice


def test_the_scalar_supplement_closes_each_documented_gap_with_provenance() -> None:
    text = (ROOT / "app/web/static/vendor/scalar-1.74.0.THIRD-PARTY.txt").read_text()
    supplement = text.split("Licenças complementares verificadas nas fontes oficiais", 1)[1]
    assert len(re.findall(r"Revisão gitHead da versão npm: [0-9a-f]{40}", supplement)) == 19
    assert len(re.findall(r"SHA-256 do texto: [0-9a-f]{64}", supplement)) == 19
    assert "@replit/codemirror-css-color-picker@6.3.0" in supplement
    assert "Não há aviso próprio de copyright" in supplement
    assert "Texto padrão MIT" in supplement and "license-list-data/v3.27.0" in supplement
    assert "Integridade do tarball npm da versão: sha512-" in supplement


def test_the_distribution_notice_preserves_copyleft_and_native_image_limits() -> None:
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    for obligation in ("LGPL-3.0-only", "MPL-2.0", "OFL-1.1", "/usr/share/doc", "código-fonte"):
        assert obligation in text
    assert "não certifica a imagem inteira como MIT" in text
