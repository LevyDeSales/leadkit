# Glossário Leadkit

**Formulário:** definição de diagnóstico em formato 1, com perguntas, identidade, consentimento,
pontuação opcional, calendário, schema do plano e templates.

**Rascunho:** definição editável no admin. Um teste de rascunho não publica nem modifica envios.

**Versão publicada:** definição imutável, fixada para cada envio. Uma nova publicação incrementa
a versão e não muda os pedidos anteriores.

**Lead / participante:** pessoa que responde ao formulário. Contato e respostas são dados privados.

**Pedido:** processamento de um envio, com estado e link assinado para resultado e PDF.

**Submissão:** respostas recebidas de uma participante, com consentimentos e a versão do formulário
que ela respondeu. Um reenvio do mesmo envio confirmado conserva essa submissão.

**Conclusão:** próximo passo apresentado à participante depois de salvar a submissão, conforme a
versão do formulário: acompanhar o resultado, receber confirmação local ou seguir a outro destino.
Não é a conclusão da geração nem uma autorização de acesso ao PDF.

**Destino de conclusão:** endereço escolhido pela operadora para o próximo passo da participante.
É distinto do destino de integração que recebe eventos e dados.

**Plano:** documento estruturado gerado pelo LLM e validado contra o schema da versão.

**Pontuação:** expressões determinísticas sobre respostas, com faixas e prioridade opcionais.

**Calendário:** fases e janelas de datas que limitam os tipos de ação permitidos.

**Imagem do formulário:** recurso fixado por hash na versão, acessado como `asset:nome`.

**Marca:** identidade externa ao conteúdo do formulário, com macros compatíveis e assets locais.

**Destino:** webhook configurado pela operadora para eventos assinados de conclusão.

**Retenção:** prazos para expirar links, remover PDFs e excluir dados do pedido.
