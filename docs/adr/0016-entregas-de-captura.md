# Entregas de captura preservam o fato, a finalidade e o destino

Captura confirma respostas sem criar pedido, plano ou PDF; por isso sua intenção de entrega fica
em outbox própria, na mesma transação da submissão, com destino concreto e revisão autorizada.
Baserow conserva uma ativação da conexão e perfis separados; webhook declara os eventos aceitos,
com default PDF legado, evitando autorizar silenciosamente um contrato novo.

O despacho assíncrono usa o Hatchet e reconciliador existentes, com ID de evento estável e epoch
por tentativa de dispatch. Repetição explícita pode revalidar a revisão atual do mesmo destino,
sem redirecionar envios antigos. Transporte e retenção serializam pela submissão; dados excluídos
não são reconstruídos pela fila, e timeout pode produzir mais de uma requisição ao receptor.
