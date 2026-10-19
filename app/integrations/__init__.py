"""Integrações de um formulário com uma planilha externa (hoje só o Baserow).

Cada pedido concluído de um formulário com a integração ativa vira uma linha numa tabela do
Baserow, por um upsert pelo código do pedido (`job.public_id`): o mesmo pedido, reenviado ou
reprocessado, atualiza a linha em vez de criar outra. O desenho é um módulo por assunto:

- `baserow`: o cliente da API do Baserow (campos da tabela, achar, criar e atualizar uma linha);
- `mapping`: o que preenche cada campo (fontes), a tradução das opções de seleção pelo rótulo, o
  relatório de antes de ativar e a montagem da linha;
- `config`: a configuração guardada por formulário (conexão, mapeamento, ativa), com o token
  cifrado e a auditoria;
- `syncs`: o estado de cada sincronização (`integration_syncs`), do pedido concluído ao reenvio;
- `push`: a sincronização de um pedido e o envio de teste.

Para outro destino de planilha, o ponto de troca é o cliente (`baserow`) e os tipos de campo que o
`mapping` conhece; a configuração, o estado das sincronizações, a task do Hatchet e o admin não sabem do
Baserow.

O token da integração e os dados da lead não vão para log, auditoria, tela, mensagem de erro nem
para o Hatchet: as tasks recebem só o id da sincronização.
"""
