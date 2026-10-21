"""Navegação da central: links abrem os detalhes existentes, sem produzir efeitos."""

from urllib.parse import urlencode

from app.admin.queries.integration_hub import HubRow


def actions(row: HubRow) -> list[tuple[str, str]]:
    """Não ofereça Baserow órfão nem teste de receptor ausente."""
    if row.connector == "baserow":
        if not row.form.exists:
            return []
        base = f"/admin/forms/{row.form_slug}/baserow"
        result = [("Configurar", base + "#conexao")]
        if row.form.profile is not None and row.receiver:
            base += "?" + urlencode({"profile": row.form.profile})
            result.append(("Mapear", base + "#mapeamento"))
            result.extend(
                [
                    ("Abrir teste", base + "#teste"),
                    ("Revisar ativação", base + "#ativacao"),
                    ("Rever envios", base + "#sincronizacoes"),
                ]
            )
        return result
    base = (
        "/admin/destinations/default"
        if row.scope_kind == "default"
        else f"/admin/destinations/forms/{row.form_slug}"
    )
    if row.form and not row.form.exists and not row.configured:
        return []
    result = [
        (
            "Definir receptor próprio" if row.serving == "default" else "Configurar",
            base + "#endereco",
        )
    ]
    if row.serving == "default":
        base = "/admin/destinations/default"
        result.append(("Configurar padrão", base + "#endereco"))
    if row.receiver:
        result.extend([("Conferir eventos", base + "#eventos"), ("Abrir teste", base + "#teste")])
    return result
