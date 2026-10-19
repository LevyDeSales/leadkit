#!/usr/bin/env bash
# Deploy de uma stack do gerador de planos: migrations, o `docker stack deploy` do README e a
# conferência de que o Swarm trocou mesmo a versão. Fica na pasta da configuração no servidor
# (~/gerador-de-planos/swarm), ao lado do stack.yaml, do app.env e do db.env. Guia: README.md.
#
#   IMAGE=gerador-planos:<commit> ./deploy.sh               # primeiro deploy e atualização
#   ROLLBACK=1 IMAGE=gerador-planos:<anterior> ./deploy.sh   # volta de versão, sem migrations
#
# O nome da stack e o domínio vêm da própria pasta, do app.env: STACK_NAME (sem a linha, vale
# gerador-planos) e o PUBLIC_BASE_URL. Um STACK ou um DOMAIN no ambiente precisam ser os mesmos, ou o
# script para sem mexer em nada. Também para, sem mexer em nada, se a stack já está no ar com o router
# de outro domínio: é a configuração de outra stack em cima desta, e o site sairia do ar.
#
# Os ajustes opcionais do stack.yaml (o limite de envios do Traefik e os limites de memória) também
# vêm do app.env; sem a linha, vale o padrão do stack.yaml. Um valor inválido para o deploy sem mexer
# em nada: o Traefik descartaria o middleware com um valor que não entende, e o router dos envios
# sairia do ar; um tamanho que o Docker não lê derrubaria o `docker stack deploy` depois das
# migrations.
#
# A API só confia no X-Forwarded-* de quem chega pela rede do Traefik: o script lê a(s) sub-rede(s) da
# rede traefik-public e passa como FORWARDED_ALLOW_IPS (o app.env pode trazer outro valor).
#
# 1. Migrations: `alembic upgrade head` com a imagem de IMAGE, num contêiner avulso na rede
#    <stack>_internal, com a versão anterior ainda no ar. No primeiro deploy, a rede e o
#    banco nascem com a stack, e as migrations rodam logo depois dela. Com ROLLBACK=1 não há
#    migrations: elas só acrescentam, e a versão anterior roda com o banco como está. Sem ele, um
#    banco com migration que a imagem não conhece para o deploy: além da volta de versão, pode ser
#    um banco de outra linha de migrations.
# 2. O `docker stack deploy` do README, com no máximo 10 min de espera. A imagem já está no servidor
#    (puxada do GHCR ou construída nele). Sem --with-registry-auth de propósito: no primeiro
#    `docker stack deploy`, o Swarm guardou o digest e as plataformas da imagem do Postgres, e
#    passar a mandar o login junto faria o Swarm recriar o banco no deploy seguinte.
# 3. Conferência, saindo com 1:
#    - se uma atualização deste deploy não terminou em `completed`. Com --detach=false, o
#      `docker stack deploy` sai com 0 mesmo quando o Swarm desfaz uma versão que não passou no
#      healthcheck;
#    - se a API ou o worker não rodam a imagem de IMAGE. Com --resolve-image changed e o mesmo
#      nome, o Swarm fica com a imagem que já tinha, então uma tag reaproveitada não troca nada.
set -euo pipefail

here="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"

if (($# > 0)); then
  echo "uso: IMAGE=<imagem> [ROLLBACK=1] $0" >&2
  exit 2
fi
: "${IMAGE:?defina IMAGE, por exemplo IMAGE=gerador-planos:<commit>}"
rollback="${ROLLBACK:-0}"
if [[ "$rollback" != 0 && "$rollback" != 1 ]]; then
  echo "deploy.sh: ROLLBACK aceita só 1 (volta de versão, sem migrations) ou 0" >&2
  exit 2
fi
for file in stack.yaml app.env db.env; do
  if [[ ! -r "$here/$file" ]]; then
    echo "deploy.sh: falta $here/$file" >&2
    exit 1
  fi
done
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "deploy.sh: a imagem $IMAGE não está neste servidor; puxe do GHCR ou construa antes (README.md)" >&2
  exit 1
fi

# O nome da stack é o da pasta: STACK_NAME no app.env, ou gerador-planos (a stack que já existe em
# produção) sem a linha. Ele entra nos nomes do Traefik (stack.yaml), e duas stacks com o mesmo nome
# fazem o Traefik descartar as duas; por isso um STACK de outra pasta no ambiente é erro.
read_app_env() {
  local value
  value="$(sed -n "s/^$1=//p" "$here/app.env" | tail -n 1)"
  printf '%s' "${value%$'\r'}"
}
stack="$(read_app_env STACK_NAME)"
stack="${stack:-gerador-planos}"
if [[ ! "$stack" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]]; then
  echo "deploy.sh: STACK_NAME=$stack no app.env não serve: só letras minúsculas, dígitos e hífen, até 40 caracteres" >&2
  exit 1
fi
if [[ -n "${STACK:-}" && "$STACK" != "$stack" ]]; then
  echo "deploy.sh: STACK=$STACK difere do STACK_NAME do app.env ($stack); nada foi mexido" >&2
  exit 1
fi
export STACK="$stack"

# O Host() do router é rótulo do serviço: um domínio errado não troca réplica nenhuma e só tira o
# site do ar. Por isso o domínio sai do PUBLIC_BASE_URL do app.env, o mesmo dos links da lead.
base_url="$(read_app_env PUBLIC_BASE_URL)"
host="${base_url#*://}"
host="${host%%/*}"
if [[ -z "$host" || "$host" == "$base_url" ]]; then
  echo "deploy.sh: o app.env precisa de PUBLIC_BASE_URL=https://<domínio>" >&2
  exit 1
fi
if [[ -n "${DOMAIN:-}" && "$DOMAIN" != "$host" ]]; then
  echo "deploy.sh: DOMAIN=$DOMAIN difere do domínio do PUBLIC_BASE_URL do app.env ($host); nada foi mexido" >&2
  exit 1
fi
DOMAIN="$host"
export IMAGE DOMAIN

# Um ajuste opcional do stack.yaml: o valor do app.env desta pasta, conferido, exportado para o
# `docker stack deploy` (que não lê o app.env, só o ambiente). Sem a linha vale o padrão do
# stack.yaml. Um valor no ambiente que difere do app.env é erro, como o STACK: a pasta é a verdade, e
# o deploy seguinte, sem aquele ambiente, desfaria a mudança.
setting() {
  local name="$1" pattern="$2" hint="$3" value
  value="$(read_app_env "$name")"
  if [[ -n "$value" && -n "${!name:-}" && "${!name}" != "$value" ]]; then
    echo "deploy.sh: $name=${!name} difere do app.env ($value); nada foi mexido" >&2
    exit 1
  fi
  value="${value:-${!name:-}}"
  if [[ -z "$value" ]]; then
    return 0
  fi
  if [[ ! "$value" =~ $pattern ]]; then
    echo "deploy.sh: $name=$value não serve: $hint; nada foi mexido" >&2
    exit 1
  fi
  export "$name=$value"
}
setting RATE_LIMIT_AVERAGE '^[1-9][0-9]{0,8}$' "um número inteiro de pedidos por minuto, 1 ou mais"
setting RATE_LIMIT_BURST '^[1-9][0-9]{0,8}$' "um número inteiro de pedidos, 1 ou mais"
for name in API_MEMORY_LIMIT API_MEMORY_RESERVATION WORKER_MEMORY_LIMIT WORKER_MEMORY_RESERVATION \
  DB_MEMORY_LIMIT DB_MEMORY_RESERVATION; do
  setting "$name" '^[1-9][0-9]{0,9}([kKmMgG][bB]?)?$' "um tamanho como 512M ou 1G (k, M ou G; sem número quebrado)"
done

# De quem a API aceita o X-Forwarded-For e o X-Forwarded-Proto: só de quem chega pela rede do Traefik
# (traefik-public), não de qualquer endereço. O IP real da lead vem daí, e é a chave do bloqueio de
# login do admin; com `*`, quem fala direto com a API escolhe o IP que ela vê. A rede é externa, com a
# sub-rede do servidor, então o script a lê aqui a cada deploy, e uma rede recriada com outra faixa não
# deixa a API sem o IP real. Um FORWARDED_ALLOW_IPS no app.env (inclusive `*`) vale no lugar.
setting FORWARDED_ALLOW_IPS '^[^[:space:]]+$' "endereços ou redes (CIDR) separados por vírgula, sem espaços, ou *"
if [[ -z "${FORWARDED_ALLOW_IPS:-}" ]]; then
  subnets="$(docker network inspect traefik-public \
    --format '{{range $i, $c := .IPAM.Config}}{{if $i}},{{end}}{{$c.Subnet}}{{end}}' 2>/dev/null || true)"
  if [[ -z "$subnets" ]]; then
    echo "deploy.sh: não achei a sub-rede da rede traefik-public para o FORWARDED_ALLOW_IPS (a rede existe neste Swarm?); crie a rede ou ponha FORWARDED_ALLOW_IPS no app.env; nada foi mexido" >&2
    exit 1
  fi
  export FORWARDED_ALLOW_IPS="$subnets"
fi
echo "deploy.sh: FORWARDED_ALLOW_IPS=$FORWARDED_ALLOW_IPS"

# A stack que já está no ar e responde por outro domínio é sinal de pasta errada (a configuração de
# outra stack, sem o STACK_NAME dela): o deploy trocaria o Host() do router e tiraria do ar o site
# que está nele. Primeiro deploy: o serviço não existe, e não há o que comparar.
live_rule="$(docker service inspect --format "{{index .Spec.Labels \"traefik.http.routers.$stack.rule\"}}" "${stack}_api" 2>/dev/null || true)"
if [[ -n "$live_rule" && "$live_rule" != *"Host(\`$DOMAIN\`)"* ]]; then
  echo "deploy.sh: a stack $stack está no ar com o router $live_rule, e o app.env desta pasta aponta para $DOMAIN; nada foi mexido" >&2
  echo "deploy.sh: pode ser a pasta de outra stack: cada uma tem a sua, com o seu STACK_NAME (README.md, \"Duas stacks no mesmo servidor\")" >&2
  exit 1
fi

# O nome fixo impede duas migrations ao mesmo tempo; o contêiner não tem mais privilégios que os
# da stack (cap_drop ALL).
migrate() {
  local output status=0
  echo "deploy.sh: migrations com $IMAGE"
  output="$(docker run --rm --name "$stack-migrate" --network "${stack}_internal" \
    --cap-drop ALL --security-opt no-new-privileges=true --env-file "$here/app.env" \
    "$IMAGE" migrate 2>&1)" || status=$?
  if ((status == 0)); then
    if [[ -n "$output" ]]; then
      printf '%s\n' "$output"
    fi
    return 0
  fi
  printf '%s\n' "$output" >&2
  if [[ "$output" == *"Can't locate revision"* ]]; then
    echo "deploy.sh: o banco tem uma migration que $IMAGE não conhece (a revisão está na linha do alembic acima)" >&2
    echo "deploy.sh: se é a volta para uma versão anterior, rode de novo com ROLLBACK=1, que não migra" >&2
  fi
  return "$status"
}

# Uma linha por serviço da stack: nome, estado e início da última atualização (vazios se ele nunca
# foi atualizado).
update_states() {
  local ids
  ids="$(docker stack services -q "$stack" 2>/dev/null || true)"
  if [[ -z "$ids" ]]; then
    return 0
  fi
  # shellcheck disable=SC2086 # um id de serviço por palavra
  docker service inspect $ids --format \
    '{{.Spec.Name}} {{with .UpdateStatus}}{{.State}} {{with .StartedAt}}{{.UnixNano}}{{end}}{{end}}'
}

first_deploy=false
if docker network inspect "${stack}_internal" >/dev/null 2>&1; then
  if ((rollback == 1)); then
    echo "deploy.sh: ROLLBACK=1, sem migrations: a versão anterior roda com o banco como está"
  elif ! migrate; then
    echo "deploy.sh: as migrations falharam; a stack não foi mexida" >&2
    exit 1
  fi
else
  first_deploy=true
fi

before="$(update_states)"
deploy=(docker stack deploy --detach=false --resolve-image changed -c "$here/stack.yaml" "$stack")
if command -v timeout >/dev/null 2>&1; then
  # --foreground: o Ctrl-C chega ao docker, em vez de ficar preso no grupo de processos do timeout.
  deploy=(timeout --foreground 10m "${deploy[@]}")
fi
status=0
"${deploy[@]}" || status=$?
if ((status == 124)); then
  echo "deploy.sh: a stack não ficou de pé em 10 min; veja docker stack ps $stack --no-trunc" >&2
fi
if ((status != 0)); then
  exit "$status"
fi

if [[ "$first_deploy" == true ]] && ((rollback == 0)) && ! migrate; then
  echo "deploy.sh: a stack subiu, mas as migrations falharam; corrija e rode o deploy.sh de novo" >&2
  exit 1
fi

# O --detach=false pode voltar um instante antes de o Swarm fechar o estado da atualização.
for _ in $(seq 15); do
  after="$(update_states)"
  if ! grep -qE ' (updating|rollback_started) ' <<<"$after"; then
    break
  fi
  sleep 2
done
if [[ -z "$after" ]]; then
  echo "deploy.sh: não achei os serviços da stack $stack depois do deploy" >&2
  exit 1
fi

failed=()
while read -r service state started; do
  if [[ -z "$state" || "$state" == completed ]]; then
    continue
  fi
  # A mesma atualização de antes do deploy: não é deste deploy.
  if grep -qxF "$service $state $started" <<<"$before"; then
    continue
  fi
  failed+=("$service ($state)")
done <<<"$after"

if ((${#failed[@]} > 0)); then
  echo "deploy.sh: a versão nova não passou no healthcheck, e o Swarm voltou para a anterior ou parou a troca: ${failed[*]}" >&2
  echo "deploy.sh: o motivo está em docker service ps --no-trunc <serviço> e docker service logs <serviço>" >&2
  exit 1
fi

# A imagem de cada réplica no ar (as que estão saindo ficam de fora) contra a que IMAGE aponta.
want="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
stale=()
for service in api worker; do
  while read -r task; do
    if [[ -z "$task" ]]; then
      continue
    fi
    container="$(docker inspect --type task --format '{{.Status.ContainerStatus.ContainerID}}' "$task")"
    if [[ -z "$container" ]]; then
      continue
    fi
    running="$(docker inspect --type container --format '{{.Image}}' "$container" 2>/dev/null || true)"
    if [[ -n "$running" && "$running" != "$want" ]]; then
      stale+=("${stack}_$service")
    fi
  done < <(docker service ps -q --no-trunc --filter desired-state=running "${stack}_$service")
done
if ((${#stale[@]} > 0)); then
  echo "deploy.sh: ${stale[*]} continua com outra imagem, não a que $IMAGE aponta agora" >&2
  echo "deploy.sh: com o mesmo nome o Swarm não troca a imagem; dê um nome novo a cada build (o hash do commit)" >&2
  exit 1
fi
echo "deploy.sh: $stack no ar com $IMAGE"
