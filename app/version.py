"""A versão do sistema: `vYYYY.MM.DD.x` (data com zeros à esquerda e contador
diário iniciado em 1), no fuso de São Paulo. Versões legadas `vAAAA.M.N` seguem legíveis.

Não é a versão do formulário (`GLOSSARY.md`): esta diz que código está no ar. A preparação manual
do CI (`.github/workflows/tests.yml`, job `release-plan`) calcula a próxima a partir das tags do
repositório (`python -m app.version`, só com a biblioteca padrão). Uma futura imagem de release
pode recebê-la por `ARG` do Dockerfile (`APP_VERSION` e `APP_BUILD_DATE`). O app só a mostra:
`describe` monta o texto do rodapé do admin. O candidato não publica versões automaticamente.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

SAO_PAULO = ZoneInfo("America/Sao_Paulo")

# O calendário é validado além do formato; o legado só participa do histórico e dos retries.
NUMBER = re.compile(r"([0-9]{4})\.(0[1-9]|1[0-2])\.(0[1-9]|[12][0-9]|3[01])\.([1-9][0-9]*)")
LEGACY_NUMBER = re.compile(r"([0-9]{4})\.([1-9]|1[0-2])\.([1-9][0-9]*)")
TAG_PREFIX = "v"
TAGS_REF = "refs/tags/"
PEELED = "^{}"


class Release(NamedTuple):
    """A versão de um build (`2026.10.17.1`, sem o `v` da tag) e o dia dele em São Paulo."""

    version: str
    built_on: date


def next_version(tags: Iterable[str], now: datetime) -> str:
    """Próxima versão do dia em São Paulo, um acima do maior contador diário existente."""
    local = _sao_paulo(now).date()
    highest = 0
    for tag in tags:
        parts = _parse(tag)
        if parts is not None and parts[:3] == (local.year, local.month, local.day):
            highest = max(highest, parts[3])
    return f"{local.year:04d}.{local.month:02d}.{local.day:02d}.{highest + 1}"


def plan_release(
    ls_remote: str, sha: str, now: datetime, *, allow_bootstrap: bool = False
) -> Release:
    """A versão do build do commit `sha`, a partir da saída de `git ls-remote --tags`.

    Um commit que já tem versão a mantém: se o job de publicação roda de novo (uma falha no meio
    dele), a imagem refeita leva a mesma tag, e não nasce um segundo release para o mesmo código.
    """
    commits = _commits_by_tag(ls_remote)
    if not any(_parse(tag) is not None for tag in commits) and not allow_bootstrap:
        raise ValueError("nenhuma tag de release: o bootstrap exige --allow-bootstrap explícito")
    kept = [
        (parts, tag)
        for tag, commit in commits.items()
        if commit == sha and (parts := _parse(tag)) is not None
    ]
    number = max(kept)[1].removeprefix(TAG_PREFIX) if kept else next_version(commits, now)
    return Release(number, _sao_paulo(now).date())


def describe(number: str | None, built_on: str | None) -> str:
    """O texto que a pessoa lê: `versão 2026.10.17.1 · 17/10/2026`. Um build que não é de release (o
    hash do commit) sai como `build a1b2c3d`, e sem nada, `dev`. `built_on` é a data ISO do build;
    uma data que não é ISO é ignorada, para um argumento errado não derrubar a página."""
    number = (number or "").strip()
    if not number:
        return "dev"
    text = f"versão {number}" if _parse(f"v{number}") is not None else f"build {number}"
    try:
        day = date.fromisoformat((built_on or "").strip())
    except ValueError:
        return text
    return f"{text} · {day.day:02d}/{day.month:02d}/{day.year}"


def operator_guide_url(number: str | None) -> str:
    """O guia da tag instalada; builds sem versão válida consultam o desenvolvimento."""
    ref = f"v{number}" if number and _parse(f"v{number}") is not None else "develop"
    return f"https://github.com/LevyDeSales/leadkit/blob/{ref}/docs/guia-da-operadora/README.md"


def main(argv: Sequence[str] | None = None) -> int:
    """`git ls-remote --tags origin | python -m app.version --sha $GITHUB_SHA`: imprime as duas
    variáveis do build (`APP_VERSION` e `APP_BUILD_DATE`), no formato do `$GITHUB_ENV`."""
    parser = argparse.ArgumentParser(
        prog="python -m app.version", description="Calcula a versão do build de um commit."
    )
    parser.add_argument("--sha", required=True, help="o commit cuja versão está sendo preparada")
    parser.add_argument(
        "--allow-bootstrap",
        action="store_true",
        help="permite preparar a primeira versão de um repositório sem tags de release",
    )
    parser.add_argument(
        "--now", type=_aware, default=None, help="ISO 8601 com fuso (padrão: agora); para testar"
    )
    args = parser.parse_args(argv)
    try:
        release = plan_release(
            sys.stdin.read(),
            args.sha,
            args.now or datetime.now(UTC),
            allow_bootstrap=args.allow_bootstrap,
        )
    except ValueError as error:
        parser.error(str(error))
    print(f"APP_VERSION={release.version}")
    print(f"APP_BUILD_DATE={release.built_on.isoformat()}")
    return 0


def _sao_paulo(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("o momento precisa de fuso: o dia da versão é o de São Paulo, não o local")
    return moment.astimezone(SAO_PAULO)


def _aware(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} não é uma data ISO 8601") from None
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r} precisa de fuso (por exemplo, +00:00)")
    return moment


def _parse(tag: str) -> tuple[int, int, int, int] | None:
    if not tag.startswith(TAG_PREFIX):
        return None
    number = tag.removeprefix(TAG_PREFIX)
    match = NUMBER.fullmatch(number)
    if match is not None:
        year, month, day, counter = map(int, match.groups())
        try:
            date(year, month, day)
        except ValueError:
            return None
        return year, month, day, counter
    legacy = LEGACY_NUMBER.fullmatch(number)
    if legacy is not None and int(legacy[1]) > 0:
        return int(legacy[1]), int(legacy[2]), 0, int(legacy[3])
    return None


def _commits_by_tag(ls_remote: str) -> dict[str, str]:
    """Tag → commit, com peeling independente da ordem e falhas de leitura explícitas."""
    refs: dict[str, str] = {}
    invalid = "saída de git ls-remote inválida; bootstrap não corrige falha de leitura"
    for line in ls_remote.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", parts[0]):
            raise ValueError(invalid)
        sha, ref = parts
        base = ref.removesuffix(PEELED)
        if (
            not base.startswith(TAGS_REF)
            or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", base)
            or ".." in base
            or "@{" in base
            or base.endswith(".")
            or any(
                not part or part.startswith(".") or part.endswith(".lock")
                for part in base.split("/")
            )
        ):
            raise ValueError(invalid)
        if ref in refs and refs[ref] != sha:
            raise ValueError(invalid)
        refs[ref] = sha
    if any(ref.endswith(PEELED) and ref.removesuffix(PEELED) not in refs for ref in refs):
        raise ValueError(invalid)
    return {
        ref.removeprefix(TAGS_REF): refs.get(f"{ref}{PEELED}", sha)
        for ref, sha in refs.items()
        if not ref.endswith(PEELED)
    }


if __name__ == "__main__":
    raise SystemExit(main())
