"""A recusa de um teste aparece no fragmento realmente substituído pelo HTMX."""

import re
from html.parser import HTMLParser
from uuid import uuid4

import pytest
from test_admin import section
from test_admin_destinations import DEFAULT, admin_client, post
from test_deployment_contract import evidence

from app import destinations
from app.deployment_contract import attest_rollout, begin_rollout


class SelectedFragment(HTMLParser):
    def __init__(self, target):
        super().__init__()
        self.target, self.depth, self.parts = target, 0, []

    def handle_starttag(self, tag, attrs):
        if self.depth or dict(attrs).get("id") == self.target:
            self.parts.append(self.get_starttag_text())
            if tag not in {"input", "br", "img", "hr", "meta", "link"}:
                self.depth += 1

    def handle_endtag(self, tag):
        if self.depth:
            self.parts.append(f"</{tag}>")
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def selected_fragment(html, target):
    parser = SelectedFragment(target)
    parser.feed(html)
    return "".join(parser.parts)


@pytest.fixture
def verified_contract(deps):
    epoch = uuid4()
    with deps.sessions.begin() as session:
        begin_rollout(session, epoch=epoch)
        attest_rollout(session, epoch=epoch, evidence=evidence())
    try:
        yield
    finally:
        with deps.sessions.begin() as session:
            begin_rollout(session, epoch=uuid4())


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "plain"])
def test_participant_probe_refusal_is_visible_in_selected_response_without_sending(
    deps, verified_contract, monkeypatch, htmx
):
    client = admin_client(deps)
    with deps.sessions.begin() as session:
        destinations.save(
            session,
            deps.cipher,
            form_slug=None,
            url="https://example.test/hook",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="participant",
        )
    calls = []
    monkeypatch.setattr(deps.webhook, "probe", lambda *args: calls.append(True))
    initial = client.get(DEFAULT)
    form = section(initial.text, "teste")
    target = re.search(r'hx-select="#([a-z-]+)"', form).group(1)
    response = post(client, f"{DEFAULT}/test", headers={"HX-Request": "true"} if htmx else {})
    assert response.status_code == (200 if htmx else 422)
    # Este é exatamente o conteúdo que o cliente HTMX seleciona, não o restante do HTML.
    visible = selected_fragment(response.text, target) if htmx else response.text
    assert "Evento de teste não enviado" in visible
    assert "Para testar participante" in visible
    assert 'role="alert"' in visible
    assert "/admin/destinations#formularios" in visible
    assert calls == []
