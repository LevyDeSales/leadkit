# syntax=docker/dockerfile:1
# Imagem única do app novo: API (FastAPI), worker do Hatchet ou migrations. O processo vem do
# argumento ou de APP_ROLE: api (padrão), worker ou migrate (deploy/entrypoint.sh).
# Deploy: app/README.md, seção "Deploy em VPS".
# As dependências saem do uv.lock, que traz o hash de cada arquivo, e a base e o uv têm digest: o
# mesmo commit gera a mesma imagem. Como atualizar tudo isso: CONTRIBUTING.md, "Dependências".

# uv 0.13.0 (o do CI). Só instala as dependências: não entra na imagem final.
FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

# python:3.11-slim-trixie em 2026-10-14.
FROM python:3.11-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

# O venv fica em /opt/venv, que vai no PATH: `python`, `uvicorn` e `alembic` saem dele.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"

# Pango e HarfBuzz para o WeasyPrint (as mesmas bibliotecas do CI); as fontes do PDF vêm com o app,
# em app/assets/fonts. tzdata: datas no fuso de São Paulo no PDF e no admin.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Primeiro só as dependências, que mudam menos que o código e ficam numa camada própria. `--locked`
# recusa um uv.lock fora de dia com o pyproject.toml; sem extras, ou seja, sem as de desenvolvimento.
COPY pyproject.toml uv.lock ./
COPY LICENSE THIRD_PARTY_NOTICES.md ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project
COPY app ./app
# Depois o pacote, instalado de verdade (não editável). O código fica também em /app/app: é dali
# que o `migrate` lê app/alembic.ini e as migrations. O cache local do uv usa os metadados do
# projeto; uma mudança só em Python/templates precisa reconstruir o wheel de leadkit, mesmo
# com pyproject.toml e versão iguais. As dependências continuam no cache da primeira etapa.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --reinstall-package leadkit \
    && rm -rf build ./*.egg-info

COPY --chmod=755 deploy/entrypoint.sh /usr/local/bin/entrypoint

# A versão do sistema (vYYYY.MM.DD.x, app/version.py) e a data do build em São Paulo, AAAA-MM-DD: o CI as
# passa em cada push em main (`--build-arg`), e o rodapé do admin as mostra. Sem elas o rodapé diz
# `dev`. Ficam depois das camadas pesadas: um valor novo não refaz as dependências nem o app.
ARG APP_VERSION=""
ARG APP_BUILD_DATE=""
ENV APP_VERSION=$APP_VERSION \
    APP_BUILD_DATE=$APP_BUILD_DATE

# Usuário sem privilégios: o código é do root e só de leitura para ele; a home guarda o cache do
# fontconfig.
RUN useradd --create-home --uid 10001 --user-group app
USER app

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint"]
