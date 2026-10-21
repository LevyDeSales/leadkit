"""A versão do sistema: `vAAAA.M.N` (ano, mês sem zero à esquerda e o número da
versão dentro do mês), com o ano e o mês do fuso de São Paulo.

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

# Sem zero à esquerda no mês nem no número: `2026.10.1`, nunca `2026.10.01`. Uma tag fora disto
# (`v2026.10.1-beta.1`, `v1.0.0`) não conta para o número seguinte.
NUMBER = re.compile(r"(\d{4})\.([1-9]|1[0-2])\.([1-9]\d*)")
TAG_PREFIX = "v"
TAGS_REF = "refs/tags/"
PEELED = "^{}"


class Release(NamedTuple):
    """A versão de um build (`2026.10.1`, sem o `v` da tag) e o dia dele em São Paulo."""

    version: str
    built_on: date


def next_version(tags: Iterable[str], now: datetime) -> str:
    """O número da próxima versão: o do mês de `now` em São Paulo, um acima do maior já tagueado
    nele, ou 1 se o mês ainda não tem nenhuma."""
    local = _sao_paulo(now)
    year, month = local.year, local.month
    highest = 0
    for tag in tags:
        parts = _parse(tag)
        if parts is not None and parts[:2] == (year, month):
            highest = max(highest, parts[2])
    return f"{year}.{month}.{highest + 1}"


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
    """O texto que a pessoa lê: `versão 2026.10.1 · 17/10/2026`. Um build que não é de release (o
    hash do commit) sai como `build a1b2c3d`, e sem nada, `dev`. `built_on` é a data ISO do build;
    uma data que não é ISO é ignorada, para um argumento errado não derrubar a página."""
    number = (number or "").strip()
    if not number:
        return "dev"
    text = f"versão {number}" if NUMBER.fullmatch(number) else f"build {number}"
    try:
        day = date.fromisoformat((built_on or "").strip())
    except ValueError:
        return text
    return f"{text} · {day.day:02d}/{day.month:02d}/{day.year}"


def operator_guide_url(number: str | None) -> str:
    """O guia da tag instalada; builds sem versão válida consultam o desenvolvimento."""
    ref = f"v{number}" if number and NUMBER.fullmatch(number) else "develop"
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
        raise ValueError("o momento precisa de fuso: o mês da versão é o de São Paulo, não o local")
    return moment.astimezone(SAO_PAULO)


def _aware(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} não é uma data ISO 8601") from None
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r} precisa de fuso (por exemplo, +00:00)")
    return moment


def _parse(tag: str) -> tuple[int, int, int] | None:
    if not tag.startswith(TAG_PREFIX):
        return None
    match = NUMBER.fullmatch(tag.removeprefix(TAG_PREFIX))
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def _commits_by_tag(ls_remote: str) -> dict[str, str]:
    """Tag → commit. Uma tag anotada lista o objeto da tag e, na linha seguinte (`nome^{}`), o
    commit: a segunda vale."""
    commits: dict[str, str] = {}
    for line in ls_remote.splitlines():
        parts = line.split()
        if not parts:
            continue
        if (
            len(parts) != 2
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", parts[0])
            or not parts[1].startswith(TAGS_REF)
        ):
            raise ValueError(
                "saída de git ls-remote inválida; bootstrap não corrige falha de leitura"
            )
        commits[parts[1].removeprefix(TAGS_REF).removesuffix(PEELED)] = parts[0]
    return commits


if __name__ == "__main__":
    raise SystemExit(main())
