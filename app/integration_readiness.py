"""Validação local da finalidade publicada e serialização com mudanças operacionais."""

import sqlalchemy as sa

from app.models import Integration, WebhookDestination
from app.persisted_webhook import V2_SELECTION


def configuration_lock(session, *, shared=False):
    """Publicação e mudanças de configuração compartilham uma trava curta, antes de outras linhas."""
    session.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    session.execute(sa.text(f"select {function}(846155001)"))


def profile_for(spec):
    return "capture" if spec is not None and spec.generation == "none" else "pdf"


def problems(session, spec, *, check_integrations=True):
    """Sem rede: fontes e chave compatíveis com a versão candidata; erro orienta ação explícita."""
    destination = session.scalar(
        sa.select(WebhookDestination)
        .where(
            sa.or_(
                WebhookDestination.form_slug == spec.slug, WebhookDestination.form_slug.is_(None)
            )
        )
        .order_by(WebhookDestination.form_slug.is_(None))
        .limit(1)
    )
    integrations = (
        list(
            session.scalars(
                sa.select(Integration).where(
                    Integration.form_slug == spec.slug, Integration.active.is_(True)
                )
            )
        )
        if check_integrations
        else []
    )
    return problems_for(spec, destination=destination, integrations=integrations)


def problems_for(spec, *, destination, integrations):
    """As mesmas regras locais sobre configurações já carregadas, sem consulta nem mutação.

    A central passa o receptor efetivo e as conexões do formulário no mesmo snapshot usado
    nas suas colunas; ``problems`` conserva a leitura e a assinatura das ações existentes.
    """
    from app.forms.definition import Problem
    from app.integrations import mapping

    profile = profile_for(spec)
    event = "submissao.persistida" if profile == "capture" else "plano.concluido"
    result = []
    if destination and profile == "capture" and destination.purpose != "internal":
        result.append(
            Problem(
                "integrations.purpose",
                "O webhook efetivo de captura exige finalidade interna. Escolha um destino interno autorizado ou remova o destino, conferindo o padrão herdado.",
            )
        )
    manual_participant = (
        profile == "pdf"
        and spec.release_mode == "manual"
        and destination is not None
        and destination.purpose == "participant"
    )
    accepted = {"plano.concluido", "plano.liberado"} if manual_participant else {event}
    if (
        destination
        and V2_SELECTION in destination.events
        and destination.purpose == "internal"
        and destination.form_slug == spec.slug
    ):
        accepted.add(V2_SELECTION)
    if destination and not accepted.intersection(destination.events):
        result.append(
            Problem(
                "integrations",
                f"O webhook efetivo não aceita {event}. Configure os eventos aceitos ou remova o destino, conferindo o padrão herdado.",
            )
        )
    for integration in integrations:
        if not integration.active:
            continue
        if integration.purpose != "internal":
            result.append(
                Problem(
                    "integrations",
                    "O Baserow exige finalidade interna. Corrija a conexão ou desative-a antes de publicar.",
                )
            )
        entries = mapping.entries_of(
            integration.capture_mapping if profile == "capture" else integration.mapping
        )
        key_field = (
            integration.capture_key_field_id if profile == "capture" else integration.key_field_id
        )
        sources = mapping.catalog(spec, profile=profile)
        key = mapping.key_entry(entries, profile=profile)
        if (
            not entries
            or key is None
            or key.field_id != key_field
            or any(
                e.source not in sources or not mapping.compatible(sources[e.source], e.type)
                for e in entries
            )
        ):
            result.append(
                Problem(
                    "integrations",
                    "A integração ativa tem perfil incompatível com esta finalidade. Prepare o mapeamento ou desative a conexão antes de publicar.",
                )
            )
    return result


def current_specs(session, slug=None):
    """Versões públicas no mesmo snapshot/transação da mudança, sem abrir outra sessão."""
    from app.forms.definition import compile_definition
    from app.forms.stored import stored_version
    from app.models import Form

    slugs = [slug] if slug is not None else session.scalars(sa.select(Form.slug)).all()
    for candidate in slugs:
        version = stored_version(session, candidate)
        if version:
            yield compile_definition(version.definition, version_number=version.number)


def webhook_problems(session, slug=None):
    """Finalidade efetiva incluindo fallback; configuração não consulta o receptor."""
    return [
        problem
        for spec in current_specs(session, slug)
        for problem in problems(session, spec, check_integrations=False)
    ]
