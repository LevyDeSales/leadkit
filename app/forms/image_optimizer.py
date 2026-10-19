"""A otimização das imagens que a operadora envia (Pillow): o que entra no storage é uma imagem
limpa, pequena e sem metadados, não o arquivo que ela mandou.

`optimize` recebe os bytes e devolve a imagem pronta (`OptimizedImage`), ou levanta `ImageRefused`
com o motivo, em português, para a tela. Só PNG, JPEG e WebP, reconhecidos pelo conteúdo e não pela
extensão. O SVG fica de fora no MVP: ele carrega script e referências, e o PDF e a página da lead
não precisam dele. GIF, imagem animada e qualquer outro formato também.

O que a otimização faz:
- tira os metadados (EXIF, XMP, perfil de cor, comentários) e a posição do GPS junto: a imagem é
  refeita a partir dos pixels. A orientação do EXIF é aplicada antes, para a foto de celular não
  ficar deitada. O perfil de cor sai: a imagem vale como sRGB, o que o navegador e o WeasyPrint
  já supõem;
- limita a maior dimensão a `MAX_DIMENSION` pixels, sem deformar (o PDF é A4 e a página é de
  celular: mais que isso só pesa);
- guarda o canal de transparência só se alguma parte da imagem é transparente;
- escolhe o formato que dá o menor arquivo: PNG tenta o PNG otimizado e o WebP sem perdas (os
  pixels não mudam); JPEG tenta o JPEG a qualidade 85 e o WebP com perdas, a 85; WebP segue WebP,
  e o arquivo original fica como está se já é limpo e cabe nos limites (recodificar um WebP com
  perdas só gastaria qualidade). O WebP só entra quando o arquivo é menor que o do formato de
  origem.

O mesmo conteúdo dá sempre os mesmos bytes: o storage guarda a imagem pelo sha256 dela
(`app.forms.definition.images.AssetRef`).
"""

from __future__ import annotations

import hashlib
import io
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 2000
# O teto de pixels do arquivo enviado, antes de abrir a imagem inteira: 40 megapixels já é uma
# foto de câmera grande, e decodificar mais que isso pesaria na memória do servidor.
MAX_PIXELS = 40_000_000
JPEG_QUALITY = 85
WEBP_QUALITY = 85
# No WebP sem perdas a "qualidade" é o esforço da compressão: 75 chega a 1,5% do tamanho de 100 em
# um quinto do tempo (medido em um PNG de 1284 × 725 com transparência).
WEBP_LOSSLESS_EFFORT = 75
FORMATS = ("PNG", "JPEG", "WEBP")
CONTENT_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
# Os pedaços de um WebP que não são a imagem: com eles, o arquivo ainda tem metadados.
_WEBP_IMAGE_CHUNKS = {"VP8 ", "VP8L", "VP8X", "ALPH"}

EMPTY = "o arquivo está vazio"
TOO_BIG = "o arquivo passa de {limit} MB; reduza a imagem antes de enviar"
NOT_AN_IMAGE = (
    "o arquivo não é uma imagem PNG, JPEG ou WebP (SVG, GIF e outros formatos não são aceitos)"
)
TOO_MANY_PIXELS = "a imagem tem pixels demais ({width} × {height}); reduza antes de enviar"
ANIMATED = "imagem animada não é aceita; envie uma imagem parada"
CORRUPT = "o arquivo da imagem está corrompido ou incompleto"


class ImageRefused(Exception):
    """A imagem não serve; a mensagem diz por quê, para a tela."""


_STRICT = threading.Lock()


@contextmanager
def _strict_decoding() -> Iterator[None]:
    """Decodifica sem perdoar arquivo cortado. O WeasyPrint liga `LOAD_TRUNCATED_IMAGES` no
    Pillow inteiro, para o PDF não cair por uma imagem estragada; aqui o que se quer é o contrário:
    um arquivo incompleto é recusado, não completado em cinza."""
    with _STRICT:
        previous = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            yield
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous


@dataclass(frozen=True)
class OptimizedImage:
    """A imagem pronta para guardar e o que mudou em relação ao arquivo enviado."""

    content: bytes
    content_type: str
    width: int
    height: int
    source_size: int
    source_type: str
    source_width: int
    source_height: int

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def resized(self) -> bool:
        return (self.width, self.height) != (self.source_width, self.source_height)

    @property
    def converted(self) -> bool:
        return self.content_type != self.source_type


def _webp_chunks(content: bytes) -> list[str]:
    """Os tipos dos pedaços (chunks) de um arquivo WebP, na ordem. Um arquivo que não é um RIFF
    WebP bem formado dá a lista até onde deu para ler."""
    chunks: list[str] = []
    position = 12  # depois de "RIFF", do tamanho e de "WEBP"
    while position + 8 <= len(content):
        chunks.append(content[position : position + 4].decode("latin-1"))
        size = int.from_bytes(content[position + 4 : position + 8], "little")
        position += 8 + size + (size & 1)  # os pedaços têm tamanho par
    return chunks


def _open(content: bytes) -> Image.Image:
    """A imagem aberta e conferida (formato, tamanho, uma só imagem, inteira), ainda no formato de
    origem. Levanta `ImageRefused`."""
    if not content:
        raise ImageRefused(EMPTY)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ImageRefused(TOO_BIG.format(limit=MAX_UPLOAD_BYTES // (1024 * 1024)))
    try:
        image = Image.open(io.BytesIO(content), formats=FORMATS)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ImageRefused(NOT_AN_IMAGE) from None
    width, height = image.size
    if width < 1 or height < 1 or width * height > MAX_PIXELS:
        raise ImageRefused(TOO_MANY_PIXELS.format(width=width, height=height))
    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
        raise ImageRefused(ANIMATED)
    try:
        with _strict_decoding():
            image.load()
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError):
        raise ImageRefused(CORRUPT) from None
    return image


def _has_transparency(image: Image.Image) -> bool:
    """Alguma parte da imagem é transparente. Uma imagem com canal alfa todo opaco não precisa
    dele."""
    if image.mode in ("RGBA", "LA", "PA"):
        low, _ = image.getchannel("A").getextrema()
        return bool(low < 255)
    return image.mode == "P" and "transparency" in image.info


def _clean(image: Image.Image, *, transparent: bool) -> Image.Image:
    """A imagem refeita a partir dos pixels, em RGB ou RGBA: sem metadados, sem paleta, sem o
    perfil de cor."""
    pixels = image.convert("RGBA" if transparent else "RGB")
    return Image.frombytes(pixels.mode, pixels.size, pixels.tobytes())


def _scaled(size: tuple[int, int]) -> tuple[int, int]:
    """As dimensões com a maior em `MAX_DIMENSION` no máximo, na mesma proporção."""
    width, height = size
    if max(width, height) <= MAX_DIMENSION:
        return size
    scale = MAX_DIMENSION / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _encoded(image: Image.Image, form: str, **options: object) -> bytes:
    out = io.BytesIO()
    image.save(out, form, **options)
    return out.getvalue()


def optimize(content: bytes) -> OptimizedImage:
    """A imagem otimizada (ver o módulo). Levanta `ImageRefused`."""
    opened = _open(content)
    source = opened.format or ""
    source_size = opened.size
    transposed = ImageOps.exif_transpose(opened)  # a posição de deitar, antes de tirar o EXIF
    transparent = _has_transparency(transposed)
    clean = _clean(transposed, transparent=transparent)
    target = _scaled(clean.size)
    resized = target != clean.size
    if resized:
        clean = clean.resize(target, Image.Resampling.LANCZOS)

    candidates: list[tuple[str, bytes]] = []
    if source == "PNG":
        candidates.append(("PNG", _encoded(clean, "PNG", optimize=True)))
        candidates.append(
            ("WEBP", _encoded(clean, "WEBP", lossless=True, quality=WEBP_LOSSLESS_EFFORT, method=6))
        )
    elif source == "JPEG":
        candidates.append(
            ("JPEG", _encoded(clean, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True))
        )
        candidates.append(("WEBP", _encoded(clean, "WEBP", quality=WEBP_QUALITY, method=6)))
    else:
        chunks = _webp_chunks(content)
        if not resized and set(chunks) <= _WEBP_IMAGE_CHUNKS:
            # Já é limpo e cabe nos limites: recodificar um WebP com perdas só gastaria qualidade.
            candidates.append(("WEBP", content))
        else:
            options: dict[str, object] = (
                {"lossless": True, "quality": WEBP_LOSSLESS_EFFORT}
                if "VP8L" in chunks
                else {"quality": WEBP_QUALITY}
            )
            candidates.append(("WEBP", _encoded(clean, "WEBP", method=6, **options)))

    form, best = min(candidates, key=lambda candidate: len(candidate[1]))
    return OptimizedImage(
        content=best,
        content_type=CONTENT_TYPES[form],
        width=clean.size[0],
        height=clean.size[1],
        source_size=len(content),
        source_type=CONTENT_TYPES[source],
        source_width=source_size[0],
        source_height=source_size[1],
    )
