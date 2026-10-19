# A tabela `jobs` faz o papel de outbox, com um cron reconciliador

Estado: adotado.

Pedidos persistidos funcionam como outbox. O reconciliador retoma trabalho interrompido e preserva idempotência.
