"""Validade estrutural mínima; não avalia conteúdo, aparência nem qualidade semântica."""

import logging
from io import BytesIO

from pypdf import PdfReader

from app.errors import FailureCode, TerminalError

# O parser pode incluir fragmentos do documento em avisos. Somente o código seguro da
# validação sai para a aplicação; o logger da dependência não propaga para logs/Hatchet.
_parser_logger = logging.getLogger("pypdf")
_parser_logger.addHandler(logging.NullHandler())
_parser_logger.propagate = False


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
            contents = page.get_contents()
            if contents is not None:
                contents.get_data()
    except Exception:  # noqa: BLE001 — o parser não pode expor conteúdo em erros
        raise TerminalError(FailureCode.RENDER_FAILED, "PDF estruturalmente inválido") from None
