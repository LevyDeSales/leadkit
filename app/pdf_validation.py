"""Validade estrutural mínima; não avalia conteúdo, aparência nem qualidade semântica."""

import logging
import zlib
from io import BytesIO

from pypdf import PdfReader, get_configuration
from pypdf.generic import ArrayObject, EncodedStreamObject, NameObject, NullObject, StreamObject

from app.errors import FailureCode, TerminalError

# O parser pode incluir fragmentos do documento em avisos. Somente o código seguro da
# validação sai para a aplicação; o logger da dependência não propaga para logs/Hatchet.
_parser_logger = logging.getLogger("pypdf")
_parser_logger.addHandler(logging.NullHandler())
_parser_logger.propagate = False


def _validate_stream(stream: StreamObject) -> None:
    """Confere Flate antes da recuperação permissiva do pypdf, só neste objeto."""
    filters = stream.get("/Filter", ArrayObject()).get_object()
    filters = filters if isinstance(filters, ArrayObject) else [filters]
    params = stream.get("/DecodeParms", NullObject()).get_object()
    params = params if isinstance(params, ArrayObject) else [params] * len(filters)
    config = get_configuration()
    if len(filters) > config.stream_filters_maximum_length or len(params) != len(filters):
        raise ValueError
    # pypdf não expõe bytes codificados numa API pública; o lock fixa esta representação.
    data = stream._data
    work = 0
    for filter_name, decode_params in zip(filters, params):
        work += len(data)
        if work > config.stream_decoding_work_maximum_length:
            raise ValueError
        if filter_name.get_object() in ("/FlateDecode", "/Fl"):
            decoder = zlib.decompressobj()
            decoder.decompress(data, config.zlib_maximum_output_length)
            if not decoder.eof or decoder.unconsumed_tail:
                raise ValueError
        stage = EncodedStreamObject()
        stage._data = data
        stage[NameObject("/Filter")] = filter_name
        stage[NameObject("/DecodeParms")] = decode_params
        data = stage.get_data()


def validate_pdf(content: bytes) -> None:
    """Exige catálogo, páginas e streams legíveis, sem reparo permissivo ou criptografia."""
    try:
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
            raise ValueError
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted or not reader.pages:
            raise ValueError
        for page in reader.pages:
            if page.mediabox.width <= 0 or page.mediabox.height <= 0:
                raise ValueError
            raw = page.get("/Contents")
            if raw is not None and not isinstance(raw.get_object(), NullObject):
                streams = raw.get_object()
                streams = streams if isinstance(streams, ArrayObject) else [streams]
                for stream in streams:
                    _validate_stream(stream.get_object())
            contents = page.get_contents()
            if contents is not None:
                contents.get_data()
    except Exception:  # noqa: BLE001 — o parser não pode expor conteúdo em erros
        raise TerminalError(FailureCode.RENDER_FAILED, "PDF estruturalmente inválido") from None
