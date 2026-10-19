// Tela de configurar o MCP (/admin/mcp). Sem este arquivo a tela funciona igual: os comandos saem com o
// marcador «cole-a-chave-aqui» (ou com a chave que acabou de ser criada), e a pessoa o troca à mão.
// Com ele, a chave guardada que a pessoa cola no passo 1 enche os comandos do passo 3, e o botão
// «Conferir» do passo 4 pergunta a este mesmo admin quem ela é.
//
// A chave colada vive só no campo da página: nunca vai a um formulário, à query string, ao
// localStorage nem a um cookie. O «Conferir» a manda a /admin/api/v1/me, da própria origem (a política
// de conteúdo do admin só deixa isso), no cabeçalho Authorization, sem cookie nem referrer.
(() => {
  "use strict";

  const KEY = /^gp_([0-9a-f]{8})_([A-Za-z0-9_-]{16,128})$/;
  const PLACEHOLDER = "cole-a-chave-aqui";
  const ROLES = { leitura: "Leitura", total: "Total" };
  const SVG = "http://www.w3.org/2000/svg";

  const byId = (id) => document.getElementById(id);
  const field = () => byId("m-segredo");
  const picker = () => byId("m-chave");

  const reveal = () => {
    document.querySelectorAll("[data-needs-js]").forEach((node) => { node.hidden = false; });
  };

  // A chave que a pessoa colou, se tem a forma de uma chave; senão, vazio.
  const pasted = () => {
    const input = field();
    const value = input ? input.value.trim() : "";
    return KEY.test(value) ? value : "";
  };

  const say = (kind, message) => {
    const state = byId("m-segredo-estado");
    if (!state) return;
    state.textContent = message;
    state.dataset.kind = kind;
  };

  const fillSlots = (key) => {
    document.querySelectorAll("[data-key-slot]").forEach((slot) => {
      slot.textContent = key || PLACEHOLDER;
      slot.dataset.filled = key ? "sim" : "nao";
    });
  };

  // Põe a chave nos comandos só se ela é a da chave escolhida no passo 1: trocar a escolha, ou colar
  // a chave de outra, volta ao marcador. Colar uma chave que existe sem ter escolhido nenhuma escolhe
  // a dela (o que também libera as opções que dependem do papel).
  const refresh = () => {
    const input = field();
    const select = picker();
    const value = input ? input.value.trim() : "";
    if (!value) {
      say("", "");
      fillSlots("");
      return;
    }
    const parsed = KEY.exec(value);
    if (!parsed) {
      say("warn", "Isso não parece uma chave do Leadkit: ela começa com gp_ e não tem espaços.");
      fillSlots("");
      return;
    }
    const option = select ? select.querySelector(`option[value="${parsed[1]}"]`) : null;
    if (!option) {
      say("warn", "Esta chave não está entre as que valem hoje: ela pode ter sido revogada ou ter vencido.");
      fillSlots("");
      return;
    }
    if (select.value !== parsed[1]) {
      select.value = parsed[1];
      select.dispatchEvent(new Event("change", { bubbles: true })); // refaz o passo 3 para essa chave
    }
    say("ok", `Chave «${option.dataset.name}» reconhecida. Os comandos do passo 3 já levam a chave.`);
    fillSlots(value);
  };

  // A chave recém-criada já vem do servidor nos comandos; este passo a põe também no campo, para o
  // «Conferir» e para as trocas do passo 3, que refazem os comandos sem o segredo.
  const adoptCreated = () => {
    const created = byId("chave-nova");
    const input = field();
    if (created && input && !input.value) input.value = created.textContent.trim();
  };

  // Os campos do passo 3 não são trocados quando os comandos se refazem (o que a pessoa digita não se
  // perde), então quem marca o campo com problema é este passo: o servidor lista os campos em
  // data-problems, e o aviso com o motivo está nos comandos.
  const FIELDS = { python: "m-python", upload_dir: "m-upload", download_dir: "m-download" };
  const markProblems = () => {
    const region = byId("configuracao");
    const bad = region ? (region.dataset.problems || "").split(" ") : [];
    Object.entries(FIELDS).forEach(([name, id]) => {
      const input = byId(id);
      if (!input) return;
      if (bad.includes(name)) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    });
  };

  const settle = () => {
    reveal();
    adoptCreated();
    refresh();
    markProblems();
  };

  document.addEventListener("input", (event) => {
    if (event.target === field()) refresh();
  });
  document.addEventListener("change", (event) => {
    if (event.target === picker()) refresh();
  });
  document.addEventListener("htmx:afterSwap", settle);

  // --- Conferir ---------------------------------------------------------------------------------

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const icon = (name) => {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG, "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  };

  const show = (kind, title, detail) => {
    const target = byId("conferir-resultado");
    const box = el("div", `alert alert--${kind}`);
    const body = el("div");
    body.append(el("strong", "alert__title", title));
    if (detail) body.append(el("p", "", detail));
    box.append(icon(kind === "ok" ? "check" : kind === "warn" ? "alert-triangle" : "alert-circle"), body);
    target.replaceChildren(box);
  };

  const refusal = (code) => ({
    invalid_credentials: "Esta chave não vale. Confira se ela foi colada inteira, sem espaços.",
    key_expired: "Esta chave venceu. Crie outra no passo 1.",
    key_revoked: "Esta chave foi revogada. Crie outra no passo 1.",
  })[code];

  const tools = (root, role, withLeadData) => {
    const count = (name) => Number(root.dataset[name]);
    if (role === "leitura" && root.dataset.toolsLeitura) {
      return `O Claude vai ter ${count("toolsLeitura")} ferramentas, todas de consulta.`;
    }
    if (role === "total" && root.dataset.toolsTotal) {
      const extra = withLeadData && root.dataset.toolsTotalLead
        ? ` (${count("toolsTotalLead")} se você liberar os dados das leads, nas opções avançadas)`
        : "";
      return `O Claude vai ter ${count("toolsTotal")} ferramentas${extra}.`;
    }
    return "";
  };

  const check = async (root, button) => {
    const key = pasted();
    if (!key) {
      show("warn", field() && field().value.trim()
        ? "A chave colada no passo 1 não tem a forma de uma chave."
        : "Cole a chave no passo 1 para conferir.");
      if (field()) field().focus();
      return;
    }
    button.disabled = true;
    show("warn", "Conferindo…");
    try {
      const response = await fetch(root.dataset.meUrl, {
        headers: { Authorization: `Bearer ${key}`, Accept: "application/json" },
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
      let body = {};
      try { body = await response.json(); } catch (_) { /* sem corpo em JSON */ }
      if (response.ok) {
        const role = ROLES[body.role] || body.role;
        const until = body.expires_at
          ? `Vale até ${new Date(body.expires_at).toLocaleDateString("pt-BR", { timeZone: "America/Sao_Paulo" })} (horário de Brasília).`
          : "Não tem validade.";
        show("ok", `A chave «${body.name}» confere.`,
          [`Papel: ${role}.`, until, tools(root, body.role, body.role === "total")].filter(Boolean).join(" "));
      } else if (response.status === 401) {
        show("error", refusal(body.code) || "Esta chave não vale.");
      } else if (response.status === 429) {
        show("warn", "Muitas tentativas seguidas.", "Espere um minuto e confira de novo.");
      } else {
        show("error", "O admin não conseguiu conferir a chave.", `Resposta ${response.status}. Tente de novo em instantes.`);
      }
    } catch (_) {
      show("error", "Não deu para falar com a API deste admin.", "Recarregue a página e tente de novo.");
    } finally {
      button.disabled = false;
    }
  };

  document.addEventListener("click", (event) => {
    const button = event.target.closest("#conferir-botao");
    if (!button) return;
    const root = byId("conferir");
    if (root) check(root, button);
  });

  settle();
})();
