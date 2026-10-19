from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.forms import FormSpec


@dataclass(frozen=True)
class RenderedPdf:
    content: bytes
    renderer: str
    page_count: int


class PdfRenderer(Protocol):
    def render(
        self, spec: FormSpec, document: dict[str, Any], context: dict[str, Any]
    ) -> RenderedPdf:
        """Gera o PDF do plano. Levanta `TerminalError(RENDER_FAILED)` em erro de template/dados."""
        ...


class FakePdfRenderer:
    """PDF mínimo e válido de uma página com o título do plano. Só para desenvolvimento e testes."""

    def render(
        self, spec: FormSpec, document: dict[str, Any], context: dict[str, Any]
    ) -> RenderedPdf:
        title = str(document.get("titulo") or spec.title)
        return RenderedPdf(content=minimal_pdf(title), renderer="fake", page_count=1)


def minimal_pdf(text: str) -> bytes:
    """Monta um PDF 1.4 de uma página com uma linha de texto (Helvetica, só ASCII)."""
    safe = text.encode("ascii", "replace").decode().replace("\\", "\\\\")
    safe = safe.replace("(", "\\(").replace(")", "\\)")[:90]
    stream = f"BT /F1 14 Tf 50 780 Td ({safe}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(out)
