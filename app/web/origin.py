"""A origem de uma URL, como o admin e a API de admin a conferem: o esquema, o host e a porta que não
é a padrão, em minúsculas, sem login, caminho nem query.

Mora aqui, e não em `app.admin.routes` (as páginas), porque quem a usa não é só a página: a conferência
de origem dos POST do admin (`PUBLIC_BASE_URL` e o `Host` do pedido) e o `<base>` que a API de admin
põe nas prévias em HTML usam a mesma conta.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def origin_of(url: str) -> str:
    """`esquema://host[:porta]` normalizado, sem a porta padrão (e o IPv6 entre colchetes); "" se não
    for uma URL, se o esquema não for `http` nem `https` ou se a porta não for um número."""
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    default = 443 if parts.scheme == "https" else 80
    suffix = f":{port}" if port and port != default else ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}{suffix}".lower()
