<!--
Título em inglês, no imperativo e com o prefixo do tipo, como um commit:
"fix: retry webhook deliveries on HTTP 425". O corpo é em português e segue a skill /pr.
Base: feature e correção vão para `develop`; promoção (`develop` → `main`) e hotfix vão para `main`.
Apague os comentários e o que não servir.
-->

## O que muda e por quê

<!--
O problema, o que mudou e o efeito para a lead, a operadora ou quem desenvolve.
Use o menor visual que explique: pseudocódigo, árvore de chamadas, árvore de arquivos, diagrama ou diff do fluxo.
-->

Ticket ou issue: <!-- .scratch/<feature>/issues/NN-slug.md ou #123 -->

## Como foi testado

- **Antes:** <!-- o teste que falhava, a saída ou o print (com dados fictícios) -->
- **Depois:** <!-- o mesmo teste passando, a saída ou o print -->

## Risco do merge

**Porta:** <!-- mão dupla (volta com um revert) ou mão única (migration que remove, dado apagado, texto de consentimento novo) -->

**Alcance:** <!-- em uma palavra: o que ou quem pode quebrar -->

## Checklist

- [ ] A suíte passa: `python -m pytest -q`, com `TEST_DATABASE_URL` num banco terminado em `_test`
- [ ] `ruff check app tests_app` e `ruff format app tests_app` sem pendências
- [ ] Nenhum segredo, token ou dado real de lead no diff, nos testes, nos prints ou nesta descrição
- [ ] Documentação atualizada onde o comportamento mudou (README, `app/README.md`, `CONTRIBUTING.md`, `GLOSSARY.md` ou uma ADR)
- [ ] Mudou o formato da definição ou o compilador: uma versão já publicada continua compilando (`format` só muda quando a versão anterior não lê), e a chave nova entrou no editor ([regras](https://github.com/LevyDeSales/leadkit/blob/main/CONTRIBUTING.md#formulários))
- [ ] Migration nova só acrescenta: o código anterior continua rodando com ela
- [ ] As tasks do Hatchet continuam recebendo e devolvendo só IDs e contagens
