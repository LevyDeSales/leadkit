// O avaliador das condições (`show_when`) das perguntas e das opções: o mesmo algoritmo de
// `Conditions.evaluate`, em app/forms/definition/conditions.py. É uma função pura sobre as perguntas
// que a página recebe (`Conditions.page_data`, num bloco JSON) e as respostas; quem mexe no DOM é o
// public.js. Quem muda a regra lá muda este arquivo no mesmo commit: tests_app/test_conditions_js.py
// roda os dois contra os casos de tests_app/fixtures/conditions_cases.json e contra respostas
// sorteadas (no navegador, `FormConditions`; no node, o `module.exports`).
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FormConditions = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Os espaços que o `str.strip()` do Python tira (o `trim()` do JavaScript tira outro conjunto).
  const SPACE = "\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
  const EDGES = new RegExp(`^[${SPACE}]+|[${SPACE}]+$`, "g");
  const strip = (text) => text.replace(EDGES, "");

  // A resposta como as regras a leem: um texto, as opções marcadas (uma lista) ou null (não
  // respondida). Sem os espaços das pontas; em branco, ou fora do tipo, não respondida. Numa escolha
  // múltipla, um texto vale como a lista dessa opção, sem repetir nenhuma.
  function normalized(kind, raw) {
    if (kind === "choices") {
      const items = typeof raw === "string" ? [raw] : Array.isArray(raw) ? raw : [];
      const chosen = new Set();
      for (const item of items) {
        const text = typeof item === "string" ? strip(item) : "";
        if (text) chosen.add(text);
      }
      return chosen.size ? [...chosen] : null;
    }
    return typeof raw === "string" ? strip(raw) || null : null;
  }

  function matches(rule, actual) {
    if (actual === null) return false;
    const wanted = rule.value;
    if (rule.operator === "in") {
      const listed = Array.isArray(wanted) ? wanted : [wanted];
      return Array.isArray(actual) ? actual.some((item) => listed.includes(item)) : listed.includes(actual);
    }
    if (typeof wanted !== "string") return false;
    if (rule.operator === "equals") return actual === wanted;
    return actual.includes(wanted); // contains: o trecho no texto, ou a opção entre as marcadas
  }

  function holds(condition, given) {
    if (!condition) return true;
    const test = (rule) => matches(rule, given.has(rule.field) ? given.get(rule.field) : null);
    return condition.mode === "all" ? condition.rules.every(test) : condition.rules.some(test);
  }

  // A resposta sem as opções que não estão disponíveis.
  function kept(answer, available) {
    if (Array.isArray(answer)) {
      const left = answer.filter((item) => available.includes(item));
      return left.length ? left : null;
    }
    return answer !== null && available.includes(answer) ? answer : null;
  }

  // O que a lead vê com estas respostas. `questions`: as perguntas na ordem da definição, cada uma
  // com `name`, `kind`, `show_when` e, nas de escolha, `options` ({value, show_when}). `answers`: o
  // que está preenchido, pelo nome da pergunta (texto, ou lista nas escolhas múltiplas). Devolve
  // `visible` e `hidden` (os nomes, na ordem) e `options` (os valores disponíveis de cada pergunta
  // de escolha visível).
  function evaluate(questions, answers) {
    const given = new Map(); // as respostas que as regras seguintes enxergam
    const visible = [];
    const hidden = [];
    const options = Object.create(null); // um nome como `__proto__` não vira o protótipo
    for (const question of questions) {
      if (!holds(question.show_when, given)) {
        hidden.push(question.name);
        continue;
      }
      visible.push(question.name);
      const has = Object.prototype.hasOwnProperty.call(answers, question.name);
      let answer = normalized(question.kind, has ? answers[question.name] : null);
      if (question.kind === "choice" || question.kind === "choices") {
        options[question.name] = (question.options || [])
          .filter((option) => holds(option.show_when, given))
          .map((option) => option.value);
        answer = kept(answer, options[question.name]);
      }
      if (answer !== null) given.set(question.name, answer);
    }
    return { visible, hidden, options };
  }

  return { evaluate };
});
