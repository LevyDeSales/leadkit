// Painel admin: o botão de copiar um endereço, como o público de um formulário. Sem este arquivo,
// ou sem a área de transferência (http fora do localhost), o botão fica escondido, e o endereço
// continua à vista num link, que dá para selecionar ou copiar pelo menu do link.
(() => {
  "use strict";
  if (!window.isSecureContext || !navigator.clipboard) return;

  const reveal = (scope) => {
    scope.querySelectorAll("button[data-copy]").forEach((button) => { button.hidden = false; });
  };

  const timers = new Map();
  const tell = (status, message, copied) => {
    if (!status) return;
    status.textContent = message;
    status.dataset.copied = copied ? "sim" : "nao";
    clearTimeout(timers.get(status));
    timers.set(status, setTimeout(() => { status.textContent = ""; }, 4000));
  };

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-copy]");
    if (!button) return;
    const link = document.getElementById(button.dataset.copy);
    const status = document.querySelector(`[data-copy-status="${button.dataset.copy}"]`);
    if (!link) return;
    navigator.clipboard.writeText(link.textContent.trim()).then(
      () => tell(status, "Copiado.", true),
      () => {
        window.getSelection().selectAllChildren(link); // sem permissão: fica selecionado
        tell(status, "Não deu para copiar: o endereço ficou selecionado.", false);
      },
    );
  });

  reveal(document);
  document.addEventListener("htmx:afterSwap", (event) => reveal(event.target));
})();

// Editor do PDF: a numeração das linhas ao lado de cada template e o salto até a linha de um
// problema. Sem este arquivo, o problema traz a linha e o texto dela, e o editor funciona igual.
(() => {
  "use strict";

  const lineCount = (text) => text.split("\n").length;

  const number = (area) => {
    const wrap = area.closest(".code");
    if (!wrap || wrap.querySelector(".code__gutter")) return;
    const gutter = document.createElement("div");
    gutter.className = "code__gutter";
    gutter.setAttribute("aria-hidden", "true");
    wrap.classList.add("code--lines");
    wrap.insertBefore(gutter, area);
    const draw = () => {
      const total = lineCount(area.value);
      let text = "";
      for (let n = 1; n <= total; n += 1) text += n + "\n";
      gutter.textContent = text;
      gutter.scrollTop = area.scrollTop;
    };
    area.addEventListener("input", draw);
    area.addEventListener("scroll", () => { gutter.scrollTop = area.scrollTop; });
    draw();
  };

  const goTo = (area, line) => {
    const lines = area.value.split("\n");
    if (line < 1 || line > lines.length) return;
    const start = lines.slice(0, line - 1).reduce((sum, text) => sum + text.length + 1, 0);
    area.focus();
    area.setSelectionRange(start, start + lines[line - 1].length);
    const height = parseFloat(getComputedStyle(area).lineHeight) || 20;
    area.scrollTop = Math.max(0, (line - 3) * height);
  };

  document.querySelectorAll("textarea[data-code]").forEach(number);
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-line]");
    if (!link) return;
    const area = document.getElementById(link.getAttribute("href").slice(1));
    if (!area || !area.matches("textarea[data-code]")) return;
    event.preventDefault();
    goTo(area, Number(link.dataset.line));
  });
})();

// Simulação administrativa: condições puras, sem persistência local ou envio público.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-simulation]").forEach((form) => {
    const fields = form.querySelector("[data-simulation-fields]");
    const block = form.querySelector("[data-simulation-conditions]");
    const rules = block ? JSON.parse(block.textContent) : null;
    const answerName = (name) => name.slice("_simulation_".length);
    const update = () => {
      const visual = form.querySelector('[name="input_mode"]:checked')?.value === "fields";
      fields.hidden = !visual;
      const answers = {};
      fields.querySelectorAll("[data-question]").forEach((question) => {
        const controls = [...question.querySelectorAll("input,textarea")];
        const name = controls[0] ? answerName(controls[0].name) : null;
        const chosen = controls.filter((control) =>
          !["radio", "checkbox"].includes(control.type) || control.checked);
        if (name) answers[name] = controls[0].type === "checkbox"
          ? chosen.map((control) => control.value) : chosen[0]?.value || "";
      });
      const state = rules && window.FormConditions ? window.FormConditions.evaluate(rules, answers) : null;
      fields.querySelectorAll("[data-question]").forEach((question) => {
        const visible = visual && (!state || !state.hidden.includes(answerName(question.dataset.question)));
        question.hidden = !visible;
        question.querySelectorAll("input,textarea").forEach((control) => {
          const option = control.closest("[data-option]");
          const available = !option || !state || !state.options[answerName(question.dataset.question)]
            || state.options[answerName(question.dataset.question)].includes(option.dataset.option);
          if (option) option.hidden = !available;
          control.disabled = !visible || !available;
        });
      });
      form.querySelector('[name="answers"]').disabled = visual;
    };
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    update();
    form.querySelector("[data-simulation-errors]")?.focus();
  });

});

// Edição local e operações de estrutura não comprovam persistência. Só a página
// recebida após salvar pode apresentar novamente o estado confirmado pelo servidor.
document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("editor-form");
  if (!form) return;
  const status = document.getElementById("editor-save-state");
  const dirty = () => {
    form.dataset.unsaved = "true";
    status.textContent = "Edição ainda não salva";
  };
  const reveal = (target) => {
    if (!target) return;
    let parent = target.parentElement;
    while (parent) {
      if (parent.matches("details")) parent.open = true;
      parent = parent.parentElement;
    }
    if (!target.matches("input, textarea, select, button, a")) target.tabIndex = -1;
    target.focus();
  };
  const generation = form.querySelector('[name="generation"]');
  const confirmation = form.querySelector('[name="confirm_generation_removal"]');
  let previousGeneration = generation.value;
  generation.addEventListener("change", (event) => {
    if (confirmation && generation.value === "none" && previousGeneration === "pdf") {
      const accepted = window.confirm("Ao salvar somente respostas, a geração, o modelo do plano e a apresentação saem deste rascunho. A versão publicada continua igual. Confirmar a troca? Cancelar conserva sua edição e configuração.");
      if (!accepted) {
        generation.value = previousGeneration;
        event.stopPropagation();
        return;
      }
      confirmation.checked = true;
    } else if (confirmation && generation.value === "pdf") {
      confirmation.checked = false;
    }
    previousGeneration = generation.value;
  });
  form.addEventListener("input", dirty);
  form.addEventListener("change", dirty);
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (form.contains(event.detail.elt)) dirty();
  });
  document.addEventListener("htmx:afterSwap", (event) => {
    if (!form.contains(event.target)) return;
    dirty();
    reveal(event.target.querySelector("[autofocus]"));
  });
  document.addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="#"]');
    if (!link) return;
    reveal(document.getElementById(link.getAttribute("href").slice(1)));
  });
  const error = document.getElementById("problemas") || document.getElementById("aviso");
  if (error) reveal(form.querySelector('[aria-invalid="true"]') || error);
});
