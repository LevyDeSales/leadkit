#!/bin/sh
# Entrypoint da imagem: o processo vem do primeiro argumento ou de APP_ROLE (padrão: api).
# Qualquer outro comando roda como veio (`docker run IMAGEM python -c ...`, `sh`).
#   api      FastAPI na porta 8000, confiando nos X-Forwarded-* vindos de FORWARDED_ALLOW_IPS
#            (padrão *: na rede do compose só o Caddy chega na API). Argumentos extras vão para o
#            uvicorn.
#   worker   worker do Hatchet (SIGTERM espera as tasks em andamento antes de sair).
#   migrate  alembic upgrade head, uma vez (o serviço `migrate` do compose).
set -eu

if [ "$#" -eq 0 ]; then
    set -- "${APP_ROLE:-api}"
fi

case "$1" in
    api)
        shift
        exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000 \
            --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}" "$@"
        ;;
    worker)
        exec python -m app.worker
        ;;
    migrate)
        exec alembic -c app/alembic.ini upgrade head
        ;;
    *)
        exec "$@"
        ;;
esac
