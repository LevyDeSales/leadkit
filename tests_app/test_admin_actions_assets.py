"""O que o admin faz com o arquivo de uma imagem enviada (`app.admin.actions.assets`): ler o que veio
até um byte além do limite, que a página (`/admin/assets`) e a API (`/admin/api/v1/assets`) dividem.

O arquivo vem de um formulário `multipart`. Nenhum dos dois lados conhece o outro: cada um recebe o
`UploadFile` do seu framework e chama a mesma função.
"""

from __future__ import annotations

import io

from starlette.datastructures import UploadFile

from app.admin.actions import assets as actions
from app.forms.image_optimizer import MAX_UPLOAD_BYTES


def upload(content: bytes, filename: str | None = "logo.png") -> UploadFile:
    return UploadFile(io.BytesIO(content), filename=filename)


def test_no_file_and_a_file_without_a_name_are_both_empty() -> None:
    """O navegador manda uma parte sem nome de arquivo quando ninguém escolheu um."""
    assert actions.content_of(None) == b""
    assert actions.content_of(upload(b"abc", filename="")) == b""
    assert actions.content_of(upload(b"abc", filename=None)) == b""


def test_a_file_that_fits_comes_whole() -> None:
    assert actions.content_of(upload(b"abc")) == b"abc"
    assert actions.content_of(upload(b"")) == b""


def test_a_file_over_the_limit_is_read_up_to_one_byte_past_it() -> None:
    """O excesso a otimização recusa ("passa de 8 MB"), sem trazer o arquivo todo para a memória."""
    content = actions.content_of(upload(b"0" * (MAX_UPLOAD_BYTES + 5000)))

    assert len(content) == MAX_UPLOAD_BYTES + 1


def test_a_file_of_exactly_the_limit_is_not_cut() -> None:
    content = b"0" * MAX_UPLOAD_BYTES

    assert actions.content_of(upload(content)) == content
