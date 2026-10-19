"""Os recursos que o template do PDF e o CSS dele pedem: só arquivos de `app/assets/`, imagens do
formulário (`asset:<nome>`, ver `app.forms.definition.images`) e `data:`.

O adaptador do PDF (`app.adapters.weasyprint_pdf`) já recusa, na hora de renderizar, qualquer outro
endereço (`AssetsOnlyFetcher`): quem decide é ele. Esta conferência só adianta o aviso, para o
editor do PDF dizer a linha do `<img src="https://...">` ao salvar, e não "o PDF não foi gerado"
na publicação. Ela lê o texto do template como está, sem executar nada dele:
- as etiquetas que carregam um arquivo (`img`, `source`, `video`, `audio`, `embed`, `object`,
  `iframe`, `input`, `image`, `use` e `link` de folha de estilo) e os `url(...)` e `@import` dos
  atributos `style`, dos `<style>` e do CSS do PDF;
- um endereço é de fora do app se tem esquema que não é `data:` nem `asset:` nem `brand:` (`https:`, `file:`...),
  se começa por `//` ou se sai da pasta (`..`);
- um `asset:<nome>` tem de ter um nome válido e, quando quem chama diz quais imagens existem
  (`assets`), uma imagem com esse nome. A prévia da página de resultado entra só nesta conferência:
  o endereço de fora que ela pedir o navegador da lead já recusa. Um nome com Jinja
  (`asset:{{ ... }}`) não dá para conferir aqui, como os outros endereços montados na execução;
- um endereço que começa por Jinja (`{{ ... }}`) não dá para conferir aqui, e fica para o
  adaptador; os comentários Jinja (`{# #}`) não contam;
- os links (`<a href>`) não carregam nada e passam.

Não é uma barreira de segurança, e sim um aviso: um template pode montar o endereço em tempo de
execução, e o adaptador o recusa.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Collection
from html.parser import HTMLParser

from app.forms.definition.document import Problem
from app.forms.definition.images import SCHEME, asset_name, name_problem

OUTSIDE = (
    "carrega um recurso de fora do app: o PDF só abre arquivos de app/assets, imagens do "
    "formulário (asset:nome) e data:"
)
UNKNOWN_IMAGE = "a imagem «{name}» não existe: envie-a em Imagens ou confira o nome"
BAD_IMAGE_NAME = "nome de imagem inválido, «{name}»: {hint}"
# As etiquetas que carregam um arquivo e os atributos onde o endereço vai.
_LOADERS = {
    "img": ("src",),
    "source": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "embed": ("src",),
    "object": ("data",),
    "iframe": ("src",),
    "input": ("src",),
    "image": ("href", "xlink:href"),
    "use": ("href", "xlink:href"),
}
# Os padrões que leem texto de quem edita (até 256 KiB) são lineares: os quantificadores que não têm o
# que devolver são possessivos (`*+`, `++`), e o endereço sem aspas não tem `(` (um `url(` dentro de
# outro é um endereço inválido em CSS), então cada `url(` que não fecha custa só até o próximo.
_URL = re.compile(r"""url\(\s*+(?:'([^']*)'|"([^"]*)"|([^()'"\s]*+))\s*+\)""", re.IGNORECASE)
_IMPORT = re.compile(r"""@import\s++(?:'([^']*)'|"([^"]*)")""", re.IGNORECASE)
_SCHEME = re.compile(r"^\s*(?:[a-z][a-z0-9+.\-]*:|//)", re.IGNORECASE)


def _outside(address: str) -> bool:
    """O endereço leva para fora de `app/assets/`. O que vem depois do primeiro `{` é Jinja e não
    conta; um endereço que começa por Jinja não se confere. Uma imagem do formulário (`asset:`) não
    é de fora: quem a confere é `_image_problem`."""
    literal = address.split("{", 1)[0]
    if not literal.strip():
        return False
    if literal.strip().lower().startswith(("data:", SCHEME, "brand:")):
        return False
    if _SCHEME.match(literal):
        return True
    return ".." in literal.replace("\\", "/").split("/")


def _css_addresses(css: str) -> list[tuple[str, int]]:
    """Os endereços de `url(...)` e `@import` de um CSS, com a linha (a partir de 1) em que cada um
    começa. A linha sai de uma busca nas posições das quebras, feitas uma vez: contar as quebras desde
    o começo a cada endereço faria 40 mil `url(a)` levarem vários segundos."""
    breaks = [position for position, char in enumerate(css) if char == "\n"]
    found = []
    for pattern in (_URL, _IMPORT):
        for match in pattern.finditer(css):
            address = next((group for group in match.groups() if group is not None), "")
            found.append((address, bisect_left(breaks, match.start()) + 1))
    return sorted(found, key=lambda item: item[1])


class _Addresses(HTMLParser):
    """Lê o template e junta os endereços que ele carrega, com a linha de cada etiqueta."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, int]] = []
        self._style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        found = {name: value or "" for name, value in attrs}
        names = _LOADERS.get(tag, ())
        if tag == "link" and "stylesheet" in found.get("rel", "").lower().split():
            names = ("href",)
        for name in names:
            if name in found:
                self.found.append((found[name], line))
        for address, _ in _css_addresses(found.get("style", "")):
            self.found.append((address, line))
        self._style = tag == "style"

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self._style = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._style = False

    def handle_data(self, data: str) -> None:
        if self._style:
            start = self.getpos()[0]
            for address, line in _css_addresses(data):
                self.found.append((address, start + line - 1))


def _without_comments(html: str) -> str:
    """O template sem os comentários Jinja (`{# ... #}`), que viram as quebras de linha que tinham: a
    linha de um problema é a do texto escrito. Um `{#` que não fecha não é comentário, e o que vem depois
    dele também não o é: a busca é por `find`, uma passada só, e não por uma expressão regular, que
    refaria a busca do `#}` a cada `{#` sem fechamento."""
    pieces: list[str] = []
    done = 0
    while (start := html.find("{#", done)) >= 0:
        end = html.find("#}", start + 2)
        if end < 0:  # nenhum `{#` daqui para a frente fecha
            break
        pieces.append(html[done:start])
        pieces.append("\n" * html.count("\n", start, end + 2))
        done = end + 2
    pieces.append(html[done:])
    return "".join(pieces)


def _html_addresses(html: str) -> list[tuple[str, int]]:
    """Os endereços do template, com a linha. Um comentário Jinja some, e as linhas dele ficam: a
    linha do problema é a do texto escrito."""
    visible = _without_comments(html)
    parser = _Addresses()
    parser.feed(visible)
    parser.close()
    return parser.found


def _image_problem(address: str, known: Collection[str] | None) -> str | None:
    """Por que o `asset:<nome>` não vale: o nome inválido ou a imagem que não existe. None: vale,
    ou é um nome montado em Jinja, que não dá para conferir."""
    found = asset_name(address)
    if found is None or not found[1]:
        return None
    name = found[0]
    hint = name_problem(name)
    if hint is not None:
        return BAD_IMAGE_NAME.format(name=name, hint=hint)
    if known is not None and name not in known:
        return UNKNOWN_IMAGE.format(name=name)
    return None


def resource_problems(
    html: str, css: str, *, preview: str = "", assets: Collection[str] | None = None
) -> list[Problem]:
    """Os endereços de fora do app no template do PDF (`pdf.html`) e no CSS dele (`pdf.css`), e as
    imagens do formulário (`asset:<nome>`) com nome inválido ou que não existem em `assets` (sem
    `assets`, só o formato do nome é conferido), no PDF, no CSS e na prévia da página de resultado
    (`preview`). Com a linha, na ordem em que aparecem."""
    problems: list[Problem] = []
    for path, addresses, check_outside in (
        ("pdf.html", _html_addresses(html), True),
        ("pdf.css", _css_addresses(css), True),
        ("preview", _html_addresses(preview), False),
    ):
        for address, line in addresses:
            if asset_name(address) is not None:
                message = _image_problem(address, assets)
            elif check_outside and _outside(address):
                message = OUTSIDE
            else:
                message = None
            if message is not None:
                problems.append(Problem(path, message, line))
    return problems


def asset_names(html: str, css: str, preview: str = "") -> list[str]:
    """Os nomes das imagens do formulário que os templates pedem (`asset:<nome>`), sem repetir, na
    ordem em que aparecem: o PDF, o CSS dele e a prévia. Um nome montado em Jinja não entra, porque
    só se sabe na renderização, e um nome de formato inválido também não."""
    names: dict[str, None] = {}
    for addresses in (_html_addresses(html), _css_addresses(css), _html_addresses(preview)):
        for address, _ in addresses:
            found = asset_name(address)
            if found is not None and found[1] and name_problem(found[0]) is None:
                names[found[0]] = None
    return list(names)
