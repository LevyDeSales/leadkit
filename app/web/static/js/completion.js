// O recibo já contém o botão sem JavaScript; só navega para a URL HTTPS conferida no servidor.
(() => {
  "use strict";
  const slug = document.body.dataset.clearDraft;
  try {
    Object.keys(window.localStorage)
      .filter((key) => key.startsWith(`rascunho:${slug}:`))
      .forEach((key) => window.localStorage.removeItem(key));
  } catch (error) { /* O recibo continua utilizável sem armazenamento local. */ }
  const link = document.querySelector("[data-completion-redirect]");
  if (link) {
    // Dá tempo para o navegador apresentar o recibo, que permanece no histórico para retorno.
    window.setTimeout(() => { window.location.assign(link.href); }, 1200);
  }
})();
