# Deploy com Docker Swarm

Este exemplo pressupõe Swarm inicializado e um Traefik gerenciado pela operadora. Confira nomes
de rede, entrypoint e certresolver em [stack.yaml](stack.yaml). Use domínio, banco e segredos
próprios; nunca execute comandos de exemplo em uma instalação existente sem revisão.

Copie `stack.yaml`, `app.env.example` e `db.env.example` para uma pasta de configuração fora do
código; renomeie os exemplos para `app.env` e `db.env`, preencha-os e limite suas permissões.
Escolha `STACK_NAME`, domínio e uma imagem fixada por versão. `DATABASE_URL` deve usar o serviço
Postgres da mesma stack e o mesmo usuário/banco de `db.env`.

Construa a imagem com `docker build -t leadkit:VERSAO .` e disponibilize-a nos nós da sua stack.
O script [deploy.sh](deploy.sh) valida a configuração, migra e atualiza os serviços. Leia o script
e `--help` antes de executá-lo. Guarde a imagem anterior e um backup verificado para rollback.
Não tente desfazer migrations manualmente sobre dados reais.

O script [backup.sh](backup.sh) usa o serviço DB da stack e grava um dump no diretório configurado.
Agende e teste restauração em banco isolado. A baseline pública é para banco vazio; uma instalação
existente exige ensaio de compatibilidade separado. API e worker devem compartilhar configuração,
storage e, se usado, o overlay de marca montado somente leitura.
