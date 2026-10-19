"""O que o admin faz com o arquivo de uma imagem enviada: ler os bytes que vieram no formulário
`multipart`. A página (`/admin/assets`) e a API (`/admin/api/v1/assets`) chamam a mesma função (ADR
0015, "A página e a API dividem os serviços"); a regra de enviar, trocar e remover a imagem é de
`app.forms.assets`, e o limite do arquivo, de `app.forms.image_optimizer`.
"""

from __future__ import annotations

from fastapi import UploadFile

from app.forms.image_optimizer import MAX_UPLOAD_BYTES


def content_of(file: UploadFile | None) -> bytes:
    """Os bytes do arquivo enviado, lidos até um byte além do limite (o excesso a otimização
    recusa, sem trazer o arquivo todo para a memória). Sem arquivo, ou com a parte sem nome que o
    navegador manda quando ninguém escolheu um, são zero bytes (a otimização recusa: "escolha o
    arquivo")."""
    if file is None or not file.filename:
        return b""
    return file.file.read(MAX_UPLOAD_BYTES + 1)
