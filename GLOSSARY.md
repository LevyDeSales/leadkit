# Glossário Leadkit

**Formulário:** definição de diagnóstico em formato 1, com perguntas, identidade, consentimento,
pontuação opcional e conclusão; quando gera PDF, também calendário, schema do plano e templates.

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

**Captura:** submissão confirmada em um formulário cuja finalidade é salvar respostas sem gerar
plano ou PDF.

**Recibo de captura:** confirmação da submissão, que permite retomar o próximo passo definido
na versão respondida sem mostrar dados da participante.

**Plano:** documento estruturado gerado pelo LLM e validado contra o schema da versão.

**Pontuação:** expressões determinísticas sobre respostas, com faixas e prioridade opcionais.

**Calendário:** fases e janelas de datas que limitam os tipos de ação permitidos.

**Imagem do formulário:** recurso fixado por hash na versão, acessado como `asset:nome`.

**Marca:** identidade externa ao conteúdo do formulário, com macros compatíveis e assets locais.

**Destino:** receptor configurado pela operadora para os eventos que ela autorizou.

**Intenção de entrega:** compromisso de notificar um destino sobre uma captura confirmada. Existe
mesmo antes da primeira tentativa de envio e conserva o fato e o destino originais.

**Entrega de captura:** envio das respostas confirmadas a um destino autorizado, sem plano ou PDF.
Sucesso significa confirmação do transporte, sem comprovar leitura pela participante.

**Perfil de mapeamento:** seleção de dados que uma integração recebe para uma finalidade: captura
ou geração de PDF. A ativação da conexão se aplica à finalidade do formulário publicado.

**Retenção:** prazos para expirar links, remover PDFs e excluir dados do pedido.

**Audiência do plano:** destinatária da representação do plano: participante recebe os campos
públicos; interna recebe o documento integral autorizado.

**Projeção do plano:** representação do documento para uma audiência, conforme a classificação
na versão do formulário que gerou o plano. Não altera o plano original nem concede acesso a ele.

**Finalidade do receptor:** escolha entre uso interno e participante para os dados enviados a uma
integração. É distinta da finalidade de geração do formulário e do destino de conclusão.

**Autorização da entrega:** vínculo entre o fato a entregar, sua tentativa, a audiência e a revisão
do mesmo receptor. Repetir uma entrega não muda sua audiência nem a redireciona.

**Política de liberação:** regra da versão respondida para conceder acesso ao plano de forma
automática ou mediante autorização da responsável.

**Liberação individual:** autorização de acesso da participante a uma tentativa e ao PDF dela.
Uma nova tentativa tem sua própria autorização.

**Notificação de liberação:** intenção de comunicar uma liberação individual a um canal participante
autorizado. Sua confirmação de transporte não comprova leitura nem recebimento pela participante.

**Capacidade interna de PDF:** permissão limitada a uma tentativa e a um receptor interno autorizado.
Não concede acesso público nem altera a liberação da participante.
**Referência de encaminhamento:** identificador opaco que correlaciona uma submissão com uma
finalidade e um receptor autorizados; não identifica a participante nem concede acesso ao plano.

**Receptor de encaminhamento:** serviço autorizado a resolver referências no âmbito de um
formulário, finalidade e revisão de configuração.

**Intenção de encaminhamento:** vínculo histórico fixado quando a submissão é confirmada,
exista ou não uma referência utilizável.

**Credencial de serviço:** capacidade que autentica um receptor para resolver referências no
âmbito autorizado; não autentica a participante nem herda permissões administrativas.

**Fonte de eventos:** serviço autorizado a declarar certos fatos externos para um formulário,
receptora e revisão histórica determinados.

**Evento externo:** declaração conservada com ocorrência, recebimento e evidência mínima. Sua
aceitação técnica não atesta a verdade comercial do relato.

**Projeção da jornada:** leitura dos fatos conservados por fonte e entidade, distinguindo fatos
locais de declarações externas, sem um estado universal de atendimento.

**Revisão da entidade externa:** sequência de estados completos atribuída pela fonte à mesma
entidade no mesmo diagnóstico, independente da ordem de chegada.

**Declaração de comparecimento:** relato de presença, ausência ou estado não informado associado
à revisão exata de um agendamento da mesma fonte. Não decorre de convite ou agendamento.
