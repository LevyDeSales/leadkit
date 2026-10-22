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


def next_action(row: HubRow) -> tuple[str, str] | None:
    """Sugere apenas uma navegação já oferecida pela associação local."""
    available = actions(row)
    if not available:
        return None

    def at(anchor: str) -> tuple[str, str]:
        return next((action for action in available if action[1].endswith(anchor)), available[0])

    if row.receiver is None:
        return available[0]
    configuration = (
        at("/admin/destinations/default#endereco") if row.serving == "default" else available[0]
    )
    if row.receiver.credential_unreadable:
        return configuration
    profile = row.form.profile if row.form else None
    if row.form and (not row.form.exists or profile is None):
        return configuration
    if row.connector == "baserow":
        mapped = row.mapping and (
            row.mapping.capture_present if profile == "capture" else row.mapping.pdf_present
        )
        if not mapped or row.form_issues:
            return at("#mapeamento")
        if row.receiver.active is False:
            return at("#ativacao")
    elif row.form_issues:
        return at("#eventos")
    # As contagens são históricas: orientam revisão, sem atribuir falhas à revisão atual.
    if profile and getattr(row.history, profile).failed:
        return at("#sincronizacoes" if row.connector == "baserow" else "#teste")
    success = row.history.last_success
    if success and success.stream == profile and success.configuration_match == "current":
        return None
    return at("#teste")
