"""A lista dos terceiros que o bundle do Scalar embute, com a licença de cada um.

    python -m app.web.scalar_licenses api-reference-<versão>.tgz

O `app/web/static/vendor/scalar-<versão>.js` é o `dist/browser/standalone.js` do pacote npm
`@scalar/api-reference`, e ele embute, minificado, uns 150 pacotes npm (Vue, CodeMirror, as
bibliotecas de markdown...). MIT, BSD e Apache pedem que o aviso de direitos e o texto da licença
acompanhem as cópias; por isso o repositório guarda, ao lado do bundle, o
`scalar-<versão>.THIRD-PARTY.txt`, que este módulo gera:

1. Lê o tarball oficial (o mesmo de que saiu o bundle: o sha256 do `standalone.js` dele tem de ser o do
   arquivo versionado) e, do `dist/browser/standalone.js.map` dele, tira cada pacote e a versão exata
   (o caminho `node_modules/.pnpm/<nome>@<versão>/...` de cada fonte). O Tailwind CSS não está no
   mapa (o bundle leva só o CSS que ele gerou), e entra pela marca `/*! tailwindcss v... | MIT License
   */` do próprio arquivo.
2. Para cada pacote, busca no registro do npm a versão exata, confere a integridade do tarball dele e
   guarda o campo `license` e o texto dos arquivos de licença da raiz do pacote (`LICENSE`, `NOTICE`...).
   Um pacote que publicou o tarball sem arquivo de licença sai assinalado, com o autor e o repositório
   que o registro diz (o texto não está no registro, e o gerador não o inventa).
3. Escreve a lista (pacote, versão, licença) e os textos, agrupando os pacotes de texto idêntico.

Os pacotes do próprio Scalar (`@scalar/*`, no monorrepo dele) estão sob a licença do Scalar, que o
`NOTICE.txt` transcreve. A saída é a mesma para a mesma entrada (ordenada), então o diff de uma troca
de versão mostra só o que mudou. O módulo só usa a biblioteca padrão; a rede é o `fetch`, que os
testes trocam.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import tarfile
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

VENDOR = Path(__file__).resolve().parent / "static" / "vendor"
REGISTRY = "https://registry.npmjs.org"
BUNDLE_IN_TARBALL = "package/dist/browser/standalone.js"
MAP_IN_TARBALL = "package/dist/browser/standalone.js.map"
COMMAND = "python -m app.web.scalar_licenses api-reference-<versão>.tgz"
# Os arquivos da raiz do pacote que levam a licença ou o aviso de direitos que ela pede.
LICENSE_FILE = re.compile(
    r"(licen[cs]e|copying|notice|copyright|unlicense)([._-].*)?", re.IGNORECASE
)
LICENSE_FILE_MAX_BYTES = 100_000  # um texto de licença tem alguns KB; mais que isso não é um
FETCH_TIMEOUT = 30
FETCH_TRIES = 3
WORKERS = 12

Fetch = Callable[[str], bytes]

# `node_modules/.pnpm/<nome>@<versão>[_peers]/node_modules/<nome>/`: o layout do pnpm, em que cada
# fonte do mapa diz o pacote e a versão exata de que veio. Nome de escopo vira `@escopo+nome`.
PNPM_SOURCE = re.compile(
    r"node_modules/\.pnpm/(?:@[^/@+]+\+)?[^/@]+@(?P<version>[^/_]+)[^/]*/node_modules/"
    r"(?P<name>@[^/]+/[^/]+|[^/@][^/]*)/"
)
TAILWIND_BANNER = re.compile(
    r"/\*! tailwindcss v(?P<version>[0-9][^ ]*) \| (?P<license>[\w. -]+?) License"
)


class ThirdPartyError(Exception):
    """A lista não pode ser gerada de forma confiável: o texto diz o que falta e o que fazer."""


@dataclass(frozen=True)
class Package:
    name: str
    version: str

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass
class Entry:
    package: Package
    license: str  # o campo `license` do registro
    files: dict[str, str] = field(default_factory=dict)  # arquivo de licença -> texto
    author: str = ""  # nome e página, sem e-mail
    repository: str = ""


def integrity_of(data: bytes, expected: str) -> bool:
    """Confere um tarball contra o `dist.integrity` (`sha512-<base64>`) do registro."""
    algorithm, _, wanted = expected.partition("-")
    if algorithm not in ("sha512", "sha384", "sha256") or not wanted:
        return False
    digest = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
    return digest == wanted


def packages_of(source_map: Mapping[str, Any]) -> list[Package]:
    """Os pacotes npm que o mapa de fontes diz estarem no bundle, com a versão exata, em ordem. Uma
    fonte `node_modules` que não segue o layout do pnpm levanta erro: o gerador não adivinha."""
    found: dict[str, set[str]] = defaultdict(set)
    for source in source_map.get("sources", []):
        if "node_modules" not in source:
            continue  # um pacote do próprio Scalar
        match = PNPM_SOURCE.search(source)
        if match is None:
            raise ThirdPartyError(
                f"não sei ler o pacote desta fonte do mapa: {source!r}. O layout do build do Scalar "
                "mudou; ajuste PNPM_SOURCE em app/web/scalar_licenses.py."
            )
        found[match["name"]].add(match["version"])
    return [Package(name, version) for name in sorted(found) for version in sorted(found[name])]


def banner_packages(bundle: str) -> list[tuple[Package, str]]:
    """Os pacotes que só aparecem numa marca de licença do próprio bundle (o Tailwind CSS, cujo CSS
    gerado está embutido). Devolve o pacote e a licença que a marca diz."""
    return [
        (Package("tailwindcss", match["version"]), match["license"])
        for match in TAILWIND_BANNER.finditer(bundle)
    ][:1]


def license_files(tarball: bytes) -> dict[str, str]:
    """Os arquivos de licença da raiz de um tarball npm (`package/LICENSE`...), nome -> texto."""
    found: dict[str, str] = {}
    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/")
            if len(parts) != 2 or not member.isfile() or not LICENSE_FILE.fullmatch(parts[1]):
                continue
            if member.size > LICENSE_FILE_MAX_BYTES:
                continue
            handle = tar.extractfile(member)
            if handle is not None:
                found[parts[1]] = handle.read().decode("utf-8", errors="replace").strip()
    return dict(sorted(found.items()))


def license_field(metadata: Mapping[str, Any]) -> str:
    """O campo `license` de uma versão do registro, na forma antiga ou na nova."""
    value = metadata.get("license")
    if value is None and metadata.get("licenses"):
        value = metadata["licenses"]
    if isinstance(value, Mapping):
        value = value.get("type")
    if isinstance(value, list):
        value = " OR ".join(
            str(item.get("type", item)) if isinstance(item, Mapping) else str(item)
            for item in value
        )
    return str(value) if value else "(não declarada)"


def author_of(value: Any) -> str:
    """O autor de uma versão do registro, `Nome (página)`; o e-mail fica de fora."""
    if isinstance(value, Mapping):
        name, url = value.get("name"), value.get("url")
    elif isinstance(value, str):
        match = re.fullmatch(r"([^<(]*?)\s*(?:<[^>]*>)?\s*(?:\(([^)]*)\))?", value.strip())
        name, url = (match[1], match[2]) if match else (value, None)
    else:
        return ""
    return f"{name} ({url})" if name and url else str(name or url or "")


def repository_of(value: Any) -> str:
    """O repositório de uma versão do registro como endereço https simples."""
    url = value.get("url") if isinstance(value, Mapping) else value
    if not isinstance(url, str):
        return ""
    url = re.sub(r"^git\+", "", url).split("#")[0].removesuffix(".git")
    return re.sub(r"^(git|ssh)://(git@)?", "https://", url).replace(
        "git@github.com:", "https://github.com/"
    )


def entry_for(package: Package, fetch: Fetch) -> Entry:
    """A licença e os textos de um pacote, do registro: a versão exata, com o tarball conferido."""
    url = f"{REGISTRY}/{quote(package.name, safe='@')}/{quote(package.version)}"
    metadata = json.loads(fetch(url))
    dist = metadata.get("dist", {})
    tarball = fetch(dist["tarball"])
    wanted = dist.get("integrity", "")
    if not integrity_of(tarball, wanted):
        raise ThirdPartyError(
            f"{package.label}: o tarball do registro não confere com o dist.integrity ({wanted!r})"
        )
    return Entry(
        package,
        license_field(metadata),
        license_files(tarball),
        author_of(metadata.get("author")),
        repository_of(metadata.get("repository")),
    )


def render(entries: Sequence[Entry], *, version: str, integrity: str, tarball_name: str) -> str:
    """O texto do arquivo: o cabeçalho, a lista e os textos de licença (os pacotes que têm o mesmo
    texto dividem uma só cópia)."""
    entries = sorted(entries, key=lambda entry: (entry.package.name, entry.package.version))
    lines = [
        f"Terceiros embutidos em scalar-{version}.js",
        "",
        f"Gerado por `{COMMAND}` a partir de {tarball_name} (@scalar/api-reference {version}, integridade",
        f"{integrity}): cada pacote npm que o dist/browser/standalone.js.map diz estar no bundle, na versão",
        "exata, com a licença que o registro do npm declara e o texto dos arquivos de licença do tarball",
        "dele (integridade conferida). Os pacotes do próprio Scalar (@scalar/*, do monorrepo dele) estão",
        "sob a licença do Scalar, transcrita em NOTICE.txt. Não edite à mão: troque o bundle e gere de novo.",
        "",
        f"Pacotes ({len(entries)})",
        "",
    ]
    width = max((len(entry.package.label) for entry in entries), default=0)
    lines += [f"  {entry.package.label.ljust(width)}  {entry.license}" for entry in entries]

    by_text: dict[str, list[Entry]] = defaultdict(list)
    without: list[Entry] = []
    for entry in entries:
        if not entry.files:
            without.append(entry)
        for name, text in entry.files.items():
            by_text[f"{name.upper()}\n{text}"].append(entry)
    if without:
        lines += [
            "",
            "Sem arquivo de licença no tarball (o texto não está no registro; vale a licença declarada)",
            "",
            *[
                f"  {entry.package.label}: {entry.license}; autor {entry.author or '(não declarado)'}; "
                f"repositório {entry.repository or '(não declarado)'}"
                for entry in without
            ],
        ]
    lines += ["", "Textos de licença", ""]
    for key, owners in sorted(by_text.items(), key=lambda item: (item[1][0].package.name, item[0])):
        name, _, text = key.partition("\n")
        lines += [
            "=" * 78,
            f"{name}, de: " + ", ".join(owner.package.label for owner in owners),
            "=" * 78,
            "",
            text,
            "",
        ]
    return "\n".join(lines).rstrip("\n") + "\n"


def generate(tarball: bytes, *, tarball_name: str, bundle: bytes, fetch: Fetch) -> tuple[str, str]:
    """Devolve a versão do Scalar e o texto do `THIRD-PARTY.txt` do tarball. Levanta `ThirdPartyError`
    se o `standalone.js` do tarball não é o `bundle` versionado."""
    with tarfile.open(fileobj=BytesIO(tarball), mode="r:gz") as tar:
        manifest = json.load(_member(tar, "package/package.json"))
        shipped = _member(tar, BUNDLE_IN_TARBALL).read()
        source_map = json.load(_member(tar, MAP_IN_TARBALL))
    if shipped != bundle:
        raise ThirdPartyError(
            "o standalone.js deste tarball não é o arquivo versionado em app/web/static/vendor/: "
            f"sha256 {hashlib.sha256(shipped).hexdigest()} no tarball, "
            f"{hashlib.sha256(bundle).hexdigest()} no repositório. Use o tarball de que o bundle saiu."
        )
    version = manifest["version"]
    packages = [(package, None) for package in packages_of(source_map)]
    packages += [
        (package, license_) for package, license_ in banner_packages(shipped.decode("utf-8"))
    ]
    with ThreadPoolExecutor(WORKERS) as pool:
        entries = list(pool.map(lambda item: entry_for(item[0], fetch), packages))
    for entry, (_, banner_license) in zip(entries, packages, strict=True):
        if banner_license is not None and banner_license not in entry.license:
            raise ThirdPartyError(
                f"{entry.package.label}: a marca do bundle diz {banner_license}, o registro diz {entry.license}"
            )
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    return version, render(entries, version=version, integrity=integrity, tarball_name=tarball_name)


def _member(tar: tarfile.TarFile, name: str) -> Any:
    handle = tar.extractfile(name)
    if handle is None:
        raise ThirdPartyError(f"o tarball não tem {name}")
    return handle


def http_fetch(url: str) -> bytes:
    """O `GET` do registro do npm, com algumas tentativas."""
    last: Exception | None = None
    for _ in range(FETCH_TRIES):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "gerador-de-planos-licenses"}
            )
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
                return bytes(response.read())
        except (urllib.error.URLError, TimeoutError) as error:
            last = error
    raise ThirdPartyError(f"não consegui buscar {url}: {last}")


def main(
    argv: Sequence[str] | None = None, *, fetch: Fetch = http_fetch, vendor: Path = VENDOR
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.web.scalar_licenses",
        description="Gera o scalar-<versão>.THIRD-PARTY.txt ao lado do bundle do Scalar.",
    )
    parser.add_argument("tarball", type=Path, help="o api-reference-<versão>.tgz oficial do npm")
    arguments = parser.parse_args(argv)
    bundles = sorted(vendor.glob("scalar-*.js"))
    if len(bundles) != 1:
        print(
            f"esperava um só scalar-<versão>.js em {vendor}, achei {len(bundles)}", file=sys.stderr
        )
        return 1
    try:
        version, text = generate(
            arguments.tarball.read_bytes(),
            tarball_name=arguments.tarball.name,
            bundle=bundles[0].read_bytes(),
            fetch=fetch,
        )
        if bundles[0].name != f"scalar-{version}.js":
            raise ThirdPartyError(
                f"o tarball é da versão {version}, e o bundle é {bundles[0].name}"
            )
    except ThirdPartyError as error:
        print(error, file=sys.stderr)
        return 1
    target = vendor / f"scalar-{version}.THIRD-PARTY.txt"
    for old in vendor.glob("scalar-*.THIRD-PARTY.txt"):
        old.unlink()
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"{target.name} atualizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
