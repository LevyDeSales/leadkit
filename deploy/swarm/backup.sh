#!/usr/bin/env bash
# Backup diário do Postgres de uma stack do gerador de planos: um pg_dump em formato custom por dia,
# com rodízio. Roda como root no servidor da stack (cron ou timer do systemd); guia em README.md.
#   STACK             nome da stack, o STACK_NAME do app.env dela (padrão gerador-planos)
#   BACKUP_DIR        destino dos dumps (padrão /srv/backups/<stack>)
#   BACKUP_KEEP_DAYS  dias guardados (padrão 14, o prazo da ADR 0011)
# Com duas stacks no servidor, cada uma tem o seu agendamento, com o seu STACK e o seu destino.
# O dump tem dados de leads: a pasta fica só para o dono (umask 077).
set -euo pipefail
umask 077

stack="${STACK:-gerador-planos}"
if [[ ! "$stack" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]]; then
  echo "backup.sh: STACK=$stack não serve: só letras minúsculas, dígitos e hífen, até 40 caracteres" >&2
  exit 1
fi
dest="${BACKUP_DIR:-/srv/backups/$stack}"
keep_days="${BACKUP_KEEP_DAYS:-14}"

container="$(docker ps -q -f "label=com.docker.swarm.service.name=${stack}_db" | head -n 1)"
if [[ -z "$container" ]]; then
  echo "contêiner do serviço ${stack}_db não encontrado" >&2
  exit 1
fi

mkdir -p "$dest"
file="$dest/mcc-$(date +%F).dump"
trap 'rm -f "$file.tmp"' EXIT
# Grava num temporário e só renomeia com o dump completo: uma falha não deixa arquivo pela metade.
docker exec "$container" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$file.tmp"
mv "$file.tmp" "$file"
find "$dest" -name 'mcc-*.dump' -mtime +"$keep_days" -delete
