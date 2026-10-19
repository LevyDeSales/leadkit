// Roda o avaliador de condições do navegador (app/web/static/js/conditions.js) num lote de
// respostas. Lê do stdin {"questions": [...], "samples": [...]} e escreve, na saída, uma lista com o
// resultado de cada amostra: {"visible": [...], "hidden": [...], "options": {...}}.
// Quem chama é tests_app/test_conditions_js.py.
"use strict";
const { evaluate } = require("../../app/web/static/js/conditions.js");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  const { questions, samples } = JSON.parse(input);
  process.stdout.write(JSON.stringify(samples.map((answers) => evaluate(questions, answers))));
});
