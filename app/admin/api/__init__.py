"""A API de admin (ADR 0015): `/admin/api/v1`, por chave de API com papel.

- `keys`: o serviço das chaves (criar, conferir um `Authorization: Bearer`, revogar, listar), que a
  página `/admin/api-keys` e a API usam.
- `application`: a sub-aplicação FastAPI que `create_app` monta em `/admin/api/v1`.
- `errors`: o formato de erro único, com o status de cada código da tabela da ADR.
- `security` e `limits`: a autenticação e o papel de cada operação (`requires`), o bloqueio por IP da
  chave errada e o balde de uso de cada chave.
- `operations` e `common`: como uma operação se declara (papel, marcas e `confirm` saem da mesma
  declaração) e as peças que todas reutilizam (lista em `{items, next_cursor}`, `expected_version`).
- `schemas/` e um módulo por recurso (`api_keys`, `jobs`, `forms`, `assets`, `drafts`, `integrations`,
  `destinations`): os modelos e as operações.
"""
