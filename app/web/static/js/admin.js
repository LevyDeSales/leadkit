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
