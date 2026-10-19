"""A lista dos terceiros que o bundle do Scalar embute (`app.web.scalar_licenses`) e o
`scalar-<versão>.THIRD-PARTY.txt` versionado ao lado dele.

O gerador roda sem rede nos testes: o `fetch` é um registro do npm de mentira, em memória, com
tarballs pequenos. O arquivo versionado é conferido pelo que dá para conferir sem o tarball oficial:
o nome e a versão, a contagem, que toda licença é permissiva e conhecida, e que o `NOTICE.txt` diz o
mesmo (a integridade do tarball e os números).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

import pytest

from app.web import scalar_licenses as sl
from app.web.templating import STATIC_DIR

VENDOR = STATIC_DIR / "vendor"
NOTICE = STATIC_DIR / "NOTICE.txt"
# Só licenças permissivas entram no bundle: uma troca de versão que traga outra (GPL, AGPL, uma
# licença que pede o código-fonte) é para alguém olhar, não para passar pelo gerador.
PERMISSIVE = {"MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"}
MIT_TEXT = (
    "MIT License\n\nCopyright (c) 2024 Fulana\n\nPermission is hereby granted, free of charge"
)


# --- um registro do npm de mentira ---------------------------------------------------------------


def tarball_of(files: dict[str, str]) -> bytes:
    """Um tarball npm: cada arquivo sob `package/`."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


class Registry:
    """O `fetch` dos testes: `add` põe um pacote (metadados e tarball) e `__call__` responde."""

    def __init__(self) -> None:
        self.responses: dict[str, bytes] = {}

    def add(
        self,
        name: str,
        version: str,
        files: dict[str, str],
        *,
        license_: object = "MIT",
        wrong_integrity: bool = False,
        **extra: object,
    ) -> None:
        tarball = tarball_of({"package.json": "{}", **files})
        url = f"https://registry.test/{name}/-/{version}.tgz"
        wanted = integrity(b"outra coisa") if wrong_integrity else integrity(tarball)
        metadata = {"license": license_, "dist": {"tarball": url, "integrity": wanted}, **extra}
        escaped = name.replace("/", "%2F")
        self.responses[f"{sl.REGISTRY}/{escaped}/{version}"] = json.dumps(metadata).encode()
        self.responses[url] = tarball

    def __call__(self, url: str) -> bytes:
        return self.responses[url]


def scalar_tarball(
    sources: list[str], *, bundle: str = "console.log(1)", banner: str = ""
) -> bytes:
    manifest = json.dumps({"name": "@scalar/api-reference", "version": "9.9.9"})
    return tarball_of(
        {
            "package.json": manifest,
            "dist/browser/standalone.js": banner + bundle,
            "dist/browser/standalone.js.map": json.dumps({"sources": sources}),
        }
    )


PNPM = "../../../../node_modules/.pnpm"


def pnpm(name: str, version: str, file: str = "dist/index.mjs") -> str:
    folder = name.replace("/", "+")
    return f"{PNPM}/{folder}@{version}/node_modules/{name}/{file}"


# --- ler o mapa de fontes ------------------------------------------------------------------------


def test_the_packages_come_from_the_pnpm_path_of_each_source_with_the_exact_version() -> None:
    source_map = {
        "sources": [
            "../../../types/dist/index.js",  # um pacote do próprio Scalar: fica de fora
            pnpm("vue", "3.5.40"),
            pnpm("vue", "3.5.40", "dist/other.js"),
            pnpm("@vue/shared", "3.5.40"),
            f"{PNPM}/unhead@2.1.12_vue@3.5.40/node_modules/unhead/dist/client.mjs",  # com peers
        ]
    }

    assert [package.label for package in sl.packages_of(source_map)] == [
        "@vue/shared@3.5.40",
        "unhead@2.1.12",
        "vue@3.5.40",
    ]


def test_two_versions_of_a_package_are_both_listed() -> None:
    source_map = {"sources": [pnpm("ms", "2.1.3"), pnpm("ms", "3.0.0")]}

    assert [package.label for package in sl.packages_of(source_map)] == ["ms@2.1.3", "ms@3.0.0"]


def test_a_node_modules_source_in_another_layout_is_refused_not_guessed() -> None:
    with pytest.raises(sl.ThirdPartyError, match="não sei ler o pacote"):
        sl.packages_of({"sources": ["../../node_modules/vue/dist/index.mjs"]})


def test_tailwind_is_found_by_the_banner_of_the_bundle_itself() -> None:
    bundle = "x;/*! tailwindcss v4.3.3 | MIT License | https://tailwindcss.com */y"

    assert sl.banner_packages(bundle) == [(sl.Package("tailwindcss", "4.3.3"), "MIT")]
    assert sl.banner_packages("sem marca nenhuma") == []


def test_the_integrity_of_a_tarball_is_checked_against_the_registry() -> None:
    data = b"conteudo"

    assert sl.integrity_of(data, integrity(data))
    assert not sl.integrity_of(data, integrity(b"outro"))
    assert not sl.integrity_of(data, "")
    assert not sl.integrity_of(data, "md5-abc")


def test_only_the_license_files_at_the_root_of_the_package_are_read() -> None:
    tarball = tarball_of(
        {
            "LICENSE": "texto A",
            "license.md": "texto B",
            "NOTICE": "aviso",
            "README.md": "nao",
            "dist/LICENSE": "dentro de uma pasta: nao",
            "COPYING.txt": "x" * (sl.LICENSE_FILE_MAX_BYTES + 1),  # grande demais para ser licença
        }
    )

    assert sl.license_files(tarball) == {
        "LICENSE": "texto A",
        "NOTICE": "aviso",
        "license.md": "texto B",
    }


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (
            {"name": "Titus", "url": "https://x.example", "email": "t@x.example"},
            "Titus (https://x.example)",
        ),
        ({"name": "Faris"}, "Faris"),
        ("Fulana <f@x.example> (https://f.example)", "Fulana (https://f.example)"),
        ("Fulana", "Fulana"),
        (None, ""),
    ],
)
def test_the_author_is_shown_without_the_email(value: object, shown: str) -> None:
    assert sl.author_of(value) == shown


@pytest.mark.parametrize(
    "value",
    [
        "git+https://github.com/a/b.git#main",
        {"type": "git", "url": "git://github.com/a/b.git"},
        "git@github.com:a/b.git",
        "https://github.com/a/b",
    ],
)
def test_the_repository_is_a_plain_https_address(value: object) -> None:
    assert sl.repository_of(value) == "https://github.com/a/b"


def test_the_license_field_comes_in_the_old_and_the_new_forms() -> None:
    assert sl.license_field({"license": "MIT"}) == "MIT"
    assert sl.license_field({"license": {"type": "ISC"}}) == "ISC"
    assert (
        sl.license_field({"licenses": [{"type": "MIT"}, {"type": "Apache-2.0"}]})
        == "MIT OR Apache-2.0"
    )
    assert sl.license_field({}) == "(não declarada)"


# --- o gerador, de ponta a ponta -----------------------------------------------------------------


def registry_with_three_packages() -> Registry:
    registry = Registry()
    registry.add(
        "vue", "3.5.40", {"LICENSE": MIT_TEXT}, author={"name": "Evan", "email": "e@x.example"}
    )
    registry.add("@vue/shared", "3.5.40", {"LICENSE": MIT_TEXT})  # o mesmo texto: uma cópia só
    registry.add(
        "micromark",
        "4.0.0",
        {"readme.md": "# micromark"},  # o tarball não traz a licença
        author={"name": "Titus", "url": "https://wooorm.com"},
        repository={"url": "git+https://github.com/micromark/micromark.git#main"},
    )
    registry.add("tailwindcss", "4.3.3", {"LICENSE": "MIT License\n\nCopyright (c) Tailwind Labs"})
    return registry


def build(
    registry: Registry, *, bundle: str = "console.log(1)", sources: list[str] | None = None
) -> tuple[str, str]:
    banner = "/*! tailwindcss v4.3.3 | MIT License | https://tailwindcss.com */"
    tarball = scalar_tarball(
        sources
        or [pnpm("vue", "3.5.40"), pnpm("@vue/shared", "3.5.40"), pnpm("micromark", "4.0.0")],
        bundle="console.log(1)",
        banner=banner,
    )
    return sl.generate(
        tarball,
        tarball_name="api-reference-9.9.9.tgz",
        bundle=(banner + bundle).encode(),
        fetch=registry,
    )


def test_the_list_has_each_package_with_version_and_license_and_the_text_once_per_copy() -> None:
    version, text = build(registry_with_three_packages())

    assert version == "9.9.9"
    assert text.startswith("Terceiros embutidos em scalar-9.9.9.js\n")
    assert "api-reference-9.9.9.tgz" in text and "integridade\nsha512-" in text
    assert "Pacotes (4)" in text
    for line in ("@vue/shared@3.5.40", "micromark@4.0.0", "tailwindcss@4.3.3", "vue@3.5.40"):
        assert re.search(rf"^  {re.escape(line)}\s+MIT$", text, re.MULTILINE), line
    # Os dois pacotes de texto idêntico dividem uma cópia, com o nome de cada um.
    assert text.count("Copyright (c) 2024 Fulana") == 1
    assert "LICENSE, de: @vue/shared@3.5.40, vue@3.5.40" in text
    assert "Copyright (c) Tailwind Labs" in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_a_package_without_a_license_file_is_flagged_with_author_and_repository() -> None:
    _, text = build(registry_with_three_packages())

    assert "Sem arquivo de licença no tarball" in text
    assert (
        "micromark@4.0.0: MIT; autor Titus (https://wooorm.com); "
        "repositório https://github.com/micromark/micromark"
    ) in text


def test_the_same_input_gives_the_same_text() -> None:
    registry = registry_with_three_packages()

    assert build(registry) == build(registry)


def test_a_bundle_that_is_not_the_one_in_the_tarball_stops_the_generation() -> None:
    with pytest.raises(sl.ThirdPartyError, match="não é o arquivo versionado"):
        build(registry_with_three_packages(), bundle="console.log(2)")


def test_a_package_tarball_that_does_not_match_the_registry_integrity_stops_the_generation() -> (
    None
):
    registry = registry_with_three_packages()
    registry.add("vue", "3.5.40", {"LICENSE": MIT_TEXT}, wrong_integrity=True)

    with pytest.raises(sl.ThirdPartyError, match="vue@3.5.40: o tarball do registro não confere"):
        build(registry)


def test_a_banner_that_disagrees_with_the_registry_stops_the_generation() -> None:
    registry = registry_with_three_packages()
    registry.add("tailwindcss", "4.3.3", {"LICENSE": "x"}, license_="GPL-3.0")

    with pytest.raises(sl.ThirdPartyError, match="a marca do bundle diz MIT"):
        build(registry)


def vendor_with(
    tmp_path: Path, bundle: str = "console.log(1)", name: str = "scalar-9.9.9.js"
) -> Path:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / name).write_text(
        "/*! tailwindcss v4.3.3 | MIT License | https://tailwindcss.com */" + bundle
    )
    return vendor


def test_main_writes_the_list_next_to_the_bundle_and_replaces_the_old_one(tmp_path: Path) -> None:
    vendor = vendor_with(tmp_path)
    (vendor / "scalar-9.9.8.THIRD-PARTY.txt").write_text("velha")
    tarball = tmp_path / "api-reference-9.9.9.tgz"
    banner = "/*! tailwindcss v4.3.3 | MIT License | https://tailwindcss.com */"
    tarball.write_bytes(scalar_tarball([pnpm("vue", "3.5.40")], banner=banner))

    code = sl.main([str(tarball)], fetch=registry_with_three_packages(), vendor=vendor)

    assert code == 0
    assert [path.name for path in sorted(vendor.glob("*.THIRD-PARTY.txt"))] == [
        "scalar-9.9.9.THIRD-PARTY.txt"
    ]
    assert "Pacotes (2)" in (vendor / "scalar-9.9.9.THIRD-PARTY.txt").read_text()


def test_main_refuses_a_tarball_of_another_version_than_the_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vendor = vendor_with(tmp_path, name="scalar-9.9.8.js")
    tarball = tmp_path / "api-reference-9.9.9.tgz"
    banner = "/*! tailwindcss v4.3.3 | MIT License | https://tailwindcss.com */"
    tarball.write_bytes(scalar_tarball([pnpm("vue", "3.5.40")], banner=banner))

    code = sl.main([str(tarball)], fetch=registry_with_three_packages(), vendor=vendor)

    assert code == 1
    assert "o tarball é da versão 9.9.9" in capsys.readouterr().err
    assert not list(vendor.glob("*.THIRD-PARTY.txt"))


def test_main_needs_exactly_one_bundle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vendor").mkdir()
    tarball = tmp_path / "api-reference-9.9.9.tgz"
    tarball.write_bytes(scalar_tarball([]))

    assert sl.main([str(tarball)], fetch=Registry(), vendor=tmp_path / "vendor") == 1
    assert "esperava um só scalar-<versão>.js" in capsys.readouterr().err


# --- o arquivo versionado ------------------------------------------------------------------------


def vendored() -> tuple[Path, str]:
    (bundle,) = sorted(VENDOR.glob("scalar-*.js"))
    version = bundle.stem.removeprefix("scalar-")
    return bundle, version


def listed(text: str) -> list[tuple[str, str]]:
    """As linhas da lista de pacotes do arquivo: (`nome@versão`, licença)."""
    block = text.split("\nPacotes (", 1)[1].split("\n\n", 2)[1]
    return [tuple(line.split()) for line in block.splitlines()]  # type: ignore[misc]


def test_the_vendored_list_is_the_one_of_the_bundle_version() -> None:
    _, version = vendored()
    (file,) = sorted(VENDOR.glob("scalar-*.THIRD-PARTY.txt"))

    assert file.name == f"scalar-{version}.THIRD-PARTY.txt"
    text = file.read_text(encoding="utf-8")
    assert text.startswith(f"Terceiros embutidos em scalar-{version}.js\n")
    assert f"@scalar/api-reference {version}" in text


def test_the_vendored_list_has_every_package_with_a_known_permissive_license() -> None:
    _, version = vendored()
    text = (VENDOR / f"scalar-{version}.THIRD-PARTY.txt").read_text(encoding="utf-8")

    packages = listed(text)
    assert len(packages) == int(re.search(r"Pacotes \((\d+)\)", text)[1])  # type: ignore[index]
    assert len(packages) > 100
    assert {"vue", "tailwindcss", "zod"} <= {label.rpartition("@")[0] for label, _ in packages}
    assert all(re.fullmatch(r".+@\d.*", label) for label, _ in packages)  # versão exata
    unknown = {license_ for _, license_ in packages} - PERMISSIVE
    assert not unknown, (
        f"licença que o gerador não conhece como permissiva: {unknown}. Confira a licença e, se "
        "puder entrar, acrescente-a a PERMISSIVE e ao NOTICE.txt."
    )
    assert "Permission is hereby granted, free of charge" in text  # o texto do MIT
    assert "Apache License" in text  # e o do Apache


def test_the_notice_agrees_with_the_vendored_list() -> None:
    bundle, version = vendored()
    text = (VENDOR / f"scalar-{version}.THIRD-PARTY.txt").read_text(encoding="utf-8")
    notice = NOTICE.read_text(encoding="utf-8")
    packages = listed(text)

    integrity_in_list = re.search(r"integridade\n(sha512-\S+?)\):", text)
    assert integrity_in_list is not None
    assert integrity_in_list[1] in notice  # o mesmo tarball
    counts = {name: sum(1 for _, found in packages if found == name) for name in PERMISSIVE}
    line = next(item for item in notice.splitlines() if item.startswith("O Scalar embute"))
    assert f"embute {len(packages)} pacotes npm" in line
    assert f"{counts['MIT']} MIT" in line and f"{counts['Apache-2.0']} Apache-2.0" in line
    assert f"{counts['ISC']} ISC" in line
    assert f"{counts['BSD-2-Clause'] + counts['BSD-3-Clause']} BSD" in line
    assert f"vendor/{bundle.stem}.THIRD-PARTY.txt" in line
