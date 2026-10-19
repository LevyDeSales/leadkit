"""A otimização das imagens enviadas: formatos aceitos, metadados, tamanho e escolha do formato.
As imagens são geradas aqui mesmo; nenhuma é de cliente."""

from __future__ import annotations

import io
import random

import pytest
from PIL import Image, ImageFile, PngImagePlugin

from app.forms import image_optimizer
from app.forms.image_optimizer import (
    ANIMATED,
    CORRUPT,
    EMPTY,
    MAX_DIMENSION,
    MAX_UPLOAD_BYTES,
    NOT_AN_IMAGE,
    ImageRefused,
    optimize,
)

EXIF_ORIENTATION = 0x0112
EXIF_GPS = 0x8825


def saved(image: Image.Image, form: str, **options: object) -> bytes:
    out = io.BytesIO()
    image.save(out, form, **options)
    return out.getvalue()


def flat_graphic(size: tuple[int, int] = (400, 300), *, alpha: bool = False) -> Image.Image:
    """Um desenho de cores chapadas, como um logo: comprime muito sem perdas."""
    image = Image.new(
        "RGBA" if alpha else "RGB", size, (200, 30, 60, 255) if alpha else (200, 30, 60)
    )
    block = Image.new(
        image.mode, (size[0] // 2, size[1] // 3), (20, 20, 120, 128 if alpha else 255)
    )
    image.paste(block, (size[0] // 4, size[1] // 3))
    return image


def photo_like(size: tuple[int, int] = (500, 400), seed: int = 7) -> Image.Image:
    """Ruído suave por cima de um degradê: o que um JPEG de câmera parece para o compressor."""
    rng = random.Random(seed)
    width, height = size
    data = bytearray()
    for y in range(height):
        for x in range(width):
            base = (x * 255 // width, y * 255 // height, (x + y) * 255 // (width + height))
            data += bytes(min(255, max(0, channel + rng.randint(-12, 12))) for channel in base)
    return Image.frombytes("RGB", size, bytes(data))


def open_result(result: image_optimizer.OptimizedImage) -> Image.Image:
    image = Image.open(io.BytesIO(result.content))
    image.load()
    return image


def has_metadata(content: bytes) -> bool:
    """O arquivo tem EXIF, XMP, perfil de cor ou texto: o que a otimização deve tirar."""
    image = Image.open(io.BytesIO(content))
    image.load()
    leftovers = {"exif", "xmp", "icc_profile", "comment", "XML:com.adobe.xmp", "Software", "text"}
    return bool(leftovers & set(image.info)) or bool(getattr(image, "text", {}))


# --- o que entra --------------------------------------------------------------------------------


@pytest.mark.parametrize("form", ["PNG", "JPEG", "WEBP"])
def test_png_jpeg_and_webp_are_accepted_by_their_content(form: str) -> None:
    result = optimize(saved(flat_graphic(), form))

    assert result.source_type == image_optimizer.CONTENT_TYPES[form]
    assert result.content_type in image_optimizer.CONTENT_TYPES.values()
    assert open_result(result).size == (400, 300)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", EMPTY),
        (b"nao sou uma imagem", NOT_AN_IMAGE),
        (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>', NOT_AN_IMAGE),
        (saved(flat_graphic(), "GIF"), NOT_AN_IMAGE),
        (saved(flat_graphic(), "BMP"), NOT_AN_IMAGE),
        (saved(flat_graphic(), "TIFF"), NOT_AN_IMAGE),
    ],
)
def test_anything_that_is_not_png_jpeg_or_webp_is_refused_with_the_reason(
    content: bytes, reason: str
) -> None:
    with pytest.raises(ImageRefused) as refused:
        optimize(content)

    assert str(refused.value) == reason


def test_the_extension_of_the_file_does_not_matter_only_the_content() -> None:
    """O admin só passa os bytes: um PNG com nome de .jpg é um PNG, e um .png que é GIF é recusado."""
    assert optimize(saved(flat_graphic(), "PNG")).source_type == "image/png"
    with pytest.raises(ImageRefused):
        optimize(saved(flat_graphic(), "GIF"))


def test_a_file_over_the_upload_limit_is_refused_before_it_is_opened() -> None:
    with pytest.raises(ImageRefused, match="8 MB"):
        optimize(b"\x89PNG" + b"0" * MAX_UPLOAD_BYTES)


def test_an_image_with_too_many_pixels_is_refused_without_decoding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_optimizer, "MAX_PIXELS", 10_000)

    with pytest.raises(ImageRefused, match="pixels demais"):
        optimize(saved(flat_graphic((200, 100)), "PNG"))


def test_a_truncated_file_is_refused_as_corrupt_even_when_weasyprint_forgives_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O WeasyPrint liga `LOAD_TRUNCATED_IMAGES` no processo todo; o envio não pode herdar isso."""
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    whole = saved(photo_like((200, 200)), "PNG")

    with pytest.raises(ImageRefused) as refused:
        optimize(whole[: len(whole) // 2])

    assert str(refused.value) == CORRUPT


def test_an_animated_image_is_refused() -> None:
    frames = [flat_graphic((50, 50)), Image.new("RGB", (50, 50), (0, 200, 0))]
    for form in ("WEBP", "PNG"):
        content = saved(
            frames[0], form, save_all=True, append_images=frames[1:], duration=100, loop=0
        )
        with pytest.raises(ImageRefused) as refused:
            optimize(content)
        assert str(refused.value) == ANIMATED


# --- o que sai ----------------------------------------------------------------------------------


def test_the_metadata_goes_away_with_the_gps_position_and_the_color_profile() -> None:
    image = flat_graphic()
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Contadora Exemplo")
    with_png_metadata = saved(image, "PNG", pnginfo=info, icc_profile=b"\x00" * 128, dpi=(300, 300))
    exif = Image.Exif()
    exif[0x010F] = "Camera Exemplo"
    exif[EXIF_GPS] = {1: "S", 2: (23.0, 33.0, 0.0)}
    with_exif = saved(photo_like((120, 90)), "JPEG", exif=exif.tobytes())

    for content in (with_png_metadata, with_exif):
        assert has_metadata(content) or b"Contadora" in content or b"Camera" in content
        result = optimize(content)
        assert not has_metadata(result.content)
        assert b"Contadora" not in result.content and b"Camera" not in result.content


def test_the_exif_orientation_is_applied_before_it_is_dropped() -> None:
    exif = Image.Exif()
    exif[EXIF_ORIENTATION] = 6  # girar 90 graus para ver certo
    deitada = saved(photo_like((160, 80)), "JPEG", exif=exif.tobytes())

    result = optimize(deitada)

    assert (result.width, result.height) == (80, 160)
    assert (result.source_width, result.source_height) == (160, 80)


def test_the_biggest_side_is_limited_and_the_proportion_stays() -> None:
    result = optimize(saved(flat_graphic((4000, 1000)), "PNG"))

    assert (result.width, result.height) == (MAX_DIMENSION, MAX_DIMENSION // 4)
    assert open_result(result).size == (result.width, result.height)
    assert result.resized and (result.source_width, result.source_height) == (4000, 1000)


def test_an_image_inside_the_limit_keeps_its_size() -> None:
    result = optimize(saved(flat_graphic((MAX_DIMENSION, 100)), "PNG"))

    assert (result.width, result.height) == (MAX_DIMENSION, 100) and not result.resized


def test_a_png_graphic_becomes_a_lossless_webp_when_that_is_smaller() -> None:
    original = flat_graphic()
    content = saved(original, "PNG")

    result = optimize(content)

    assert result.content_type == "image/webp" and result.converted
    assert result.size < len(content)
    assert list(open_result(result).convert("RGB").getdata()) == list(original.getdata())


def test_a_png_stays_png_when_webp_would_not_be_smaller() -> None:
    """Ruído não comprime: o PNG otimizado fica uns bytes menor que o WebP sem perdas, e o formato
    não muda. A conferência da diferença avisa se uma versão nova do Pillow inverter o caso."""
    rng = random.Random(1)
    noise = Image.frombytes("RGB", (16, 16), bytes(rng.randrange(256) for _ in range(16 * 16 * 3)))
    as_png = saved(noise, "PNG", optimize=True)
    assert len(as_png) < len(saved(noise, "WEBP", lossless=True, quality=75, method=6))

    result = optimize(saved(noise, "PNG"))

    assert result.content_type == "image/png" and not result.converted
    assert result.content == as_png


def test_the_transparency_survives_the_conversion() -> None:
    result = optimize(saved(flat_graphic(alpha=True), "PNG"))

    image = open_result(result)
    assert image.mode == "RGBA"
    assert image.getchannel("A").getextrema() == (128, 255)


def test_an_alpha_channel_that_is_all_opaque_is_dropped() -> None:
    opaque = Image.new("RGBA", (200, 100), (10, 20, 30, 255))

    result = optimize(saved(opaque, "PNG"))

    assert open_result(result).mode == "RGB"


def test_a_palette_png_with_transparency_keeps_the_transparent_color() -> None:
    palette = Image.new("P", (40, 40), 0)
    palette.putpalette([255, 0, 0, 0, 0, 255] + [0] * 250 * 3)
    palette.paste(1, (10, 10, 30, 30))
    content = saved(palette, "PNG", transparency=0)

    image = open_result(optimize(content))

    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0 and image.getpixel((20, 20))[3] == 255


def test_a_cmyk_jpeg_comes_out_as_rgb() -> None:
    cmyk = Image.new("CMYK", (60, 60), (0, 255, 255, 0))

    image = open_result(optimize(saved(cmyk, "JPEG")))

    assert image.mode == "RGB"
    red, green, blue = image.getpixel((30, 30))
    assert red > 200 and green < 80 and blue < 80


def test_a_jpeg_photo_gets_smaller_when_it_was_saved_with_more_quality_than_needed() -> None:
    content = saved(photo_like(), "JPEG", quality=98, subsampling=0)

    result = optimize(content)

    assert result.size < len(content) * 0.7
    assert result.content_type in ("image/jpeg", "image/webp")


def test_a_clean_webp_inside_the_limits_is_kept_byte_for_byte() -> None:
    clean = saved(photo_like((300, 200)), "WEBP", quality=80)

    result = optimize(clean)

    assert result.content == clean and result.content_type == "image/webp"


def test_a_webp_with_metadata_is_rewritten_without_it() -> None:
    exif = Image.Exif()
    exif[0x010F] = "Camera Exemplo"
    dirty = saved(photo_like((300, 200)), "WEBP", quality=80, exif=exif.tobytes())
    assert b"EXIF" in dirty

    result = optimize(dirty)

    assert b"EXIF" not in result.content and b"Camera" not in result.content


def test_a_lossless_webp_stays_lossless_when_it_is_resized() -> None:
    wide = saved(flat_graphic((3000, 600)), "WEBP", lossless=True)

    result = optimize(wide)

    assert result.resized and b"VP8L" in result.content[:32] + result.content[12:40]


def test_the_same_upload_always_gives_the_same_bytes() -> None:
    content = saved(photo_like((200, 150)), "JPEG")

    assert optimize(content).sha256 == optimize(content).sha256
    assert optimize(content).sha256 == optimize(content).sha256


def test_the_result_tells_the_numbers_for_the_screen() -> None:
    content = saved(flat_graphic((3000, 1500)), "PNG")

    result = optimize(content)

    assert result.source_size == len(content) and result.size == len(result.content)
    assert result.source_type == "image/png" and (result.width, result.height) == (2000, 1000)
    assert len(result.sha256) == 64


@pytest.mark.parametrize("mode", ["L", "I;16", "LA"])
def test_grayscale_and_sixteen_bit_pngs_come_out_as_plain_rgb(mode: str) -> None:
    result = optimize(saved(Image.new(mode, (30, 30), 100), "PNG"))

    assert open_result(result).mode in ("RGB", "RGBA")
