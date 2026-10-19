"""Os avisos das páginas de espera e de falha: do bloco `notices` da definição para o que a tela
pública consome (`Screen.notices`). A conferência do formato (título e texto, o link com rótulo e
endereço https) está em `app.forms.definition.document`; aqui a definição conferida vira o contrato.
"""

from __future__ import annotations

from app.forms import Notice, NoticeLink, Notices
from app.forms.definition.document import NoticeDoc, NoticesDoc


def _notice(doc: NoticeDoc | None) -> Notice | None:
    if doc is None:
        return None
    link = NoticeLink(doc.link.label, doc.link.url) if doc.link is not None else None
    return Notice(doc.title, doc.text, link)


def notices_of(doc: NoticesDoc | None) -> Notices:
    """Os avisos do contrato; sem o bloco na definição, nenhum."""
    if doc is None:
        return Notices()
    return Notices(waiting=_notice(doc.waiting), failed=_notice(doc.failed))
