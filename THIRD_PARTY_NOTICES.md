# Avisos de terceiros

O código próprio do Leadkit é distribuído sob MIT (veja `LICENSE`). Essa licença não substitui
as licenças de bibliotecas, fontes e componentes incorporados. Os nomes internos do módulo `app`
e das variáveis de integração são preservados por compatibilidade.

## Arquivos incorporados ao pacote

- **htmx 2.0.11**: Zero-Clause BSD (0BSD), arquivo original de licença em
  `app/web/static/vendor/htmx-2.0.11.LICENSE.txt`, obtido da [tag oficial v2.0.11](https://github.com/bigskysoftware/htmx/blob/v2.0.11/LICENSE).
  O hash do JavaScript está em `app/web/static/NOTICE.txt`; não houve alteração do bundle.
- **Scalar API Reference 1.74.0**: MIT, com copyright e licença integral em
  `app/web/static/NOTICE.txt`. O bundle incorpora 157 pacotes npm (146 MIT, 7 Apache-2.0, 2 ISC,
  2 BSD); a lista, versões, integridade do tarball e textos estão em
  `app/web/static/vendor/scalar-1.74.0.THIRD-PARTY.txt`. O complemento registra URLs, gitHead
  das versões npm exatas e SHA-256 das 19 licenças antes ausentes dos tarballs.
  Para `@replit/codemirror-css-color-picker@6.3.0`, a licença MIT e a autoria Faris Masad
  são declaradas nos metadados oficiais; pacote e revisão não fornecem aviso próprio de
  copyright/arquivo LICENSE. Incluímos o texto MIT padrão SPDX, explicitamente identificado
  e com marcadores sem preenchimento, sem atribuir ano ou titular de copyright presumidos.
- **Source Sans Pro / Source Serif Pro**: SIL Open Font License 1.1 (OFL-1.1), Adobe Systems
  Incorporated, Reserved Font Name `Source`. Os oito TTF originais conferem byte a byte com
  `font-source-sans-pro==0.0.1` e `font-source-serif-pro==0.0.1` do PyPI. Textos integrais,
  copyright, URLs de origem e hashes dos arquivos estão em `app/assets/fonts/NOTICE.txt` e
  `LICENSE-SourceSansPro.txt` / `LICENSE-SourceSerifPro.txt` na mesma pasta. A OFL continua
  aplicada às fontes; não transforma os documentos PDF gerados em obras sob OFL.

A interface web do candidato usa fontes de sistema; Montserrat WOFF2 e o kit comercial anterior
não fazem parte da distribuição pública. As fontes PDF acima são independentes daquele kit.

## Dependências Python

O wheel Leadkit declara dependências; ele não incorpora os wheels de terceiros. A instalação
obtém suas distribuições, com seus próprios arquivos `LICENSE`, `COPYING`, `NOTICE` e metadados
em `*.dist-info`. Preserve-os ao redistribuir um ambiente ou imagem. As versões a seguir são
as fixadas no `uv.lock` desta preparação, incluindo extras de teste/MCP e marcadores de plataforma;
nem todos os pacotes são instalados na imagem sem extras. Os links levam aos metadados oficiais
da versão no PyPI. Classificadores genéricos não substituem a licença integral do artefato.

**Não se trata de uma lista somente de licenças permissivas:** `psycopg` e `psycopg-binary`
3.3.6 declaram LGPL-3.0-only; `certifi` 2026.7.22 declara MPL-2.0; `pyphen` 0.18.1 declara
alternativas GPL/LGPL/MPL, e seus dicionários têm avisos próprios. Use as opções e obrigações
expressas pelos respectivos arquivos, sem relicenciá-los como MIT.

| Pacote | Versão | Licença declarada / metadados |
| --- | --- | --- |
| [aiohappyeyeballs](https://pypi.org/pypi/aiohappyeyeballs/2.7.1/json) | 2.7.1 | PSF-2.0 |
| [aiohttp](https://pypi.org/pypi/aiohttp/3.14.5/json) | 3.14.5 | Apache-2.0 AND MIT |
| [aiosignal](https://pypi.org/pypi/aiosignal/1.4.0/json) | 1.4.0 | Apache 2.0 |
| [alembic](https://pypi.org/pypi/alembic/1.20.0/json) | 1.20.0 | MIT |
| [annotated-doc](https://pypi.org/pypi/annotated-doc/0.0.5/json) | 0.0.5 | MIT |
| [annotated-types](https://pypi.org/pypi/annotated-types/0.8.0/json) | 0.8.0 | MIT |
| [anyio](https://pypi.org/pypi/anyio/4.15.1/json) | 4.15.1 | MIT |
| [argon2-cffi](https://pypi.org/pypi/argon2-cffi/25.1.0/json) | 25.1.0 | MIT |
| [argon2-cffi-bindings](https://pypi.org/pypi/argon2-cffi-bindings/26.1.0/json) | 26.1.0 | MIT |
| [attrs](https://pypi.org/pypi/attrs/26.1.0/json) | 26.1.0 | MIT |
| [boto3](https://pypi.org/pypi/boto3/1.43.113/json) | 1.43.113 | Apache-2.0 |
| [botocore](https://pypi.org/pypi/botocore/1.43.113/json) | 1.43.113 | Apache-2.0 |
| [brotli](https://pypi.org/pypi/brotli/1.2.1/json) | 1.2.1 | MIT |
| [brotlicffi](https://pypi.org/pypi/brotlicffi/1.2.1.0/json) | 1.2.1.0 | MIT |
| [certifi](https://pypi.org/pypi/certifi/2026.7.22/json) | 2026.7.22 | MPL-2.0 |
| [cffi](https://pypi.org/pypi/cffi/2.1.1/json) | 2.1.1 | MIT-0 |
| [charset-normalizer](https://pypi.org/pypi/charset-normalizer/3.5.2/json) | 3.5.2 | MIT |
| [click](https://pypi.org/pypi/click/8.5.0/json) | 8.5.0 | BSD-3-Clause |
| [colorama](https://pypi.org/pypi/colorama/0.4.6/json) | 0.4.6 | BSD License |
| [cryptography](https://pypi.org/pypi/cryptography/50.0.2/json) | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| [cssselect2](https://pypi.org/pypi/cssselect2/0.10.1/json) | 0.10.1 | BSD License |
| [execnet](https://pypi.org/pypi/execnet/2.1.2/json) | 2.1.2 | MIT |
| [fastapi](https://pypi.org/pypi/fastapi/0.143.1/json) | 0.143.1 | MIT |
| [fonttools](https://pypi.org/pypi/fonttools/4.67.0/json) | 4.67.0 | MIT |
| [frozenlist](https://pypi.org/pypi/frozenlist/1.8.0/json) | 1.8.0 | Apache-2.0 |
| [googleapis-common-protos](https://pypi.org/pypi/googleapis-common-protos/1.75.5/json) | 1.75.5 | Apache-2.0 |
| [grpcio](https://pypi.org/pypi/grpcio/1.84.0/json) | 1.84.0 | Apache-2.0 |
| [grpcio-status](https://pypi.org/pypi/grpcio-status/1.84.0/json) | 1.84.0 | Apache-2.0 |
| [grpcio-tools](https://pypi.org/pypi/grpcio-tools/1.81.1/json) | 1.81.1 | Apache-2.0 |
| [h11](https://pypi.org/pypi/h11/0.16.0/json) | 0.16.0 | MIT |
| [hatchet-sdk](https://pypi.org/pypi/hatchet-sdk/1.41.1/json) | 1.41.1 | MIT |
| [httpcore](https://pypi.org/pypi/httpcore/1.0.9/json) | 1.0.9 | BSD-3-Clause |
| [httpcore2](https://pypi.org/pypi/httpcore2/2.13.1/json) | 2.13.1 | BSD-3-Clause |
| [httptools](https://pypi.org/pypi/httptools/0.9.0/json) | 0.9.0 | MIT |
| [httpx](https://pypi.org/pypi/httpx/0.28.1/json) | 0.28.1 | BSD-3-Clause |
| [httpx-sse](https://pypi.org/pypi/httpx-sse/0.4.3/json) | 0.4.3 | MIT |
| [httpx2](https://pypi.org/pypi/httpx2/2.13.1/json) | 2.13.1 | BSD-3-Clause |
| [httpx2-jsfetch](https://pypi.org/pypi/httpx2-jsfetch/1.0/json) | 1.0 | BSD-3-Clause |
| [idna](https://pypi.org/pypi/idna/3.20/json) | 3.20 | BSD-3-Clause |
| [iniconfig](https://pypi.org/pypi/iniconfig/2.3.1/json) | 2.3.1 | MIT |
| [itsdangerous](https://pypi.org/pypi/itsdangerous/2.2.0/json) | 2.2.0 | BSD License |
| [jinja2](https://pypi.org/pypi/jinja2/3.1.6/json) | 3.1.6 | BSD License |
| [jiter](https://pypi.org/pypi/jiter/0.17.0/json) | 0.17.0 | MIT |
| [jmespath](https://pypi.org/pypi/jmespath/1.1.0/json) | 1.1.0 | MIT |
| [jsonpatch](https://pypi.org/pypi/jsonpatch/1.33/json) | 1.33 | Modified BSD License |
| [jsonpointer](https://pypi.org/pypi/jsonpointer/3.0.0/json) | 3.0.0 | Modified BSD License |
| [jsonschema](https://pypi.org/pypi/jsonschema/4.26.0/json) | 4.26.0 | MIT |
| [jsonschema-specifications](https://pypi.org/pypi/jsonschema-specifications/2025.9.1/json) | 2025.9.1 | MIT |
| [mako](https://pypi.org/pypi/mako/1.4.3/json) | 1.4.3 | MIT |
| [markupsafe](https://pypi.org/pypi/markupsafe/3.0.4/json) | 3.0.4 | BSD-3-Clause |
| [mcp](https://pypi.org/pypi/mcp/1.30.0/json) | 1.30.0 | MIT |
| [moto](https://pypi.org/pypi/moto/5.2.4/json) | 5.2.4 | Apache-2.0 |
| [multidict](https://pypi.org/pypi/multidict/7.1.0/json) | 7.1.0 | Apache-2.0 |
| [openai](https://pypi.org/pypi/openai/3.31.0/json) | 3.31.0 | Apache-2.0 |
| [opentelemetry-api](https://pypi.org/pypi/opentelemetry-api/1.45.1/json) | 1.45.1 | Apache-2.0 |
| [packaging](https://pypi.org/pypi/packaging/26.3/json) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [pillow](https://pypi.org/pypi/pillow/12.3.0/json) | 12.3.0 | MIT-CMU |
| [pluggy](https://pypi.org/pypi/pluggy/1.6.0/json) | 1.6.0 | MIT |
| [prometheus-client](https://pypi.org/pypi/prometheus-client/0.26.0/json) | 0.26.0 | Apache-2.0 AND BSD-2-Clause |
| [propcache](https://pypi.org/pypi/propcache/0.5.4/json) | 0.5.4 | Apache-2.0 |
| [protobuf](https://pypi.org/pypi/protobuf/6.33.6/json) | 6.33.6 | 3-Clause BSD License |
| [psycopg](https://pypi.org/pypi/psycopg/3.3.6/json) | 3.3.6 | LGPL-3.0-only |
| [psycopg-binary](https://pypi.org/pypi/psycopg-binary/3.3.6/json) | 3.3.6 | LGPL-3.0-only |
| [py-partiql-parser](https://pypi.org/pypi/py-partiql-parser/0.6.3/json) | 0.6.3 | MIT |
| [pycparser](https://pypi.org/pypi/pycparser/3.11/json) | 3.11 | BSD-3-Clause |
| [pydantic](https://pypi.org/pypi/pydantic/2.14.1/json) | 2.14.1 | MIT |
| [pydantic-core](https://pypi.org/pypi/pydantic-core/2.50.1/json) | 2.50.1 | MIT |
| [pydantic-settings](https://pypi.org/pypi/pydantic-settings/2.16.0/json) | 2.16.0 | MIT |
| [pydyf](https://pypi.org/pypi/pydyf/0.13.0/json) | 0.13.0 | BSD License |
| [pygments](https://pypi.org/pypi/pygments/2.21.0/json) | 2.21.0 | BSD-2-Clause |
| [pyjwt](https://pypi.org/pypi/pyjwt/2.15.1/json) | 2.15.1 | MIT |
| [pypdf](https://pypi.org/pypi/pypdf/6.21.2/json) | 6.21.2 | BSD-3-Clause |
| [pyphen](https://pypi.org/pypi/pyphen/0.18.1/json) | 0.18.1 | GNU General Public License v2 or later (GPLv2+); GNU Lesser General Public License v2 or later (LGPLv2+); Mozilla Public License 1.1 (MPL 1.1) |
| [pytest](https://pypi.org/pypi/pytest/9.1.1/json) | 9.1.1 | MIT |
| [pytest-xdist](https://pypi.org/pypi/pytest-xdist/3.8.0/json) | 3.8.0 | MIT |
| [python-dateutil](https://pypi.org/pypi/python-dateutil/2.9.0.post0/json) | 2.9.0.post0 | Dual License |
| [python-dotenv](https://pypi.org/pypi/python-dotenv/1.2.4/json) | 1.2.4 | BSD-3-Clause |
| [python-multipart](https://pypi.org/pypi/python-multipart/0.0.32/json) | 0.0.32 | Apache-2.0 |
| [pywin32](https://pypi.org/pypi/pywin32/312/json) | 312 | PSF |
| [pyyaml](https://pypi.org/pypi/pyyaml/6.0.3/json) | 6.0.3 | MIT |
| [referencing](https://pypi.org/pypi/referencing/0.37.0/json) | 0.37.0 | MIT |
| [regex](https://pypi.org/pypi/regex/2026.9.29/json) | 2026.9.29 | Apache-2.0 AND CNRI-Python |
| [requests](https://pypi.org/pypi/requests/2.34.2/json) | 2.34.2 | Apache-2.0 |
| [responses](https://pypi.org/pypi/responses/0.26.3/json) | 0.26.3 | Apache-2.0 |
| [rpds-py](https://pypi.org/pypi/rpds-py/2026.6.3/json) | 2026.6.3 | MIT |
| [ruff](https://pypi.org/pypi/ruff/0.17.0/json) | 0.17.0 | MIT |
| [s3transfer](https://pypi.org/pypi/s3transfer/0.19.2/json) | 0.19.2 | Apache License 2.0 |
| [setuptools](https://pypi.org/pypi/setuptools/84.1.0/json) | 84.1.0 | MIT |
| [six](https://pypi.org/pypi/six/1.17.0/json) | 1.17.0 | MIT |
| [sniffio](https://pypi.org/pypi/sniffio/1.3.1/json) | 1.3.1 | MIT OR Apache-2.0 |
| [sqlalchemy](https://pypi.org/pypi/sqlalchemy/2.1.4/json) | 2.1.4 | MIT |
| [sse-starlette](https://pypi.org/pypi/sse-starlette/3.5.0/json) | 3.5.0 | BSD-3-Clause |
| [starlette](https://pypi.org/pypi/starlette/1.8.0/json) | 1.8.0 | BSD-3-Clause |
| [tenacity](https://pypi.org/pypi/tenacity/9.2.1/json) | 9.2.1 | Apache-2.0 |
| [tinycss2](https://pypi.org/pypi/tinycss2/1.5.1/json) | 1.5.1 | BSD License |
| [tinyhtml5](https://pypi.org/pypi/tinyhtml5/2.1.0/json) | 2.1.0 | MIT License |
| [truststore](https://pypi.org/pypi/truststore/0.10.5/json) | 0.10.5 | MIT |
| [typing-extensions](https://pypi.org/pypi/typing-extensions/4.16.0/json) | 4.16.0 | PSF-2.0 |
| [typing-inspection](https://pypi.org/pypi/typing-inspection/0.4.4/json) | 0.4.4 | MIT |
| [tzdata](https://pypi.org/pypi/tzdata/2026.5/json) | 2026.5 | Apache-2.0 |
| [urllib3](https://pypi.org/pypi/urllib3/2.8.0/json) | 2.8.0 | MIT |
| [uvicorn](https://pypi.org/pypi/uvicorn/0.54.0/json) | 0.54.0 | BSD-3-Clause |
| [uvloop](https://pypi.org/pypi/uvloop/0.23.0/json) | 0.23.0 | MIT License |
| [watchfiles](https://pypi.org/pypi/watchfiles/1.3.0/json) | 1.3.0 | MIT |
| [weasyprint](https://pypi.org/pypi/weasyprint/70.0/json) | 70.0 | BSD License |
| [webencodings](https://pypi.org/pypi/webencodings/0.6.1/json) | 0.6.1 | BSD License |
| [websockets](https://pypi.org/pypi/websockets/17.2/json) | 17.2 | BSD-3-Clause |
| [werkzeug](https://pypi.org/pypi/werkzeug/3.1.9/json) | 3.1.9 | BSD-3-Clause |
| [xmltodict](https://pypi.org/pypi/xmltodict/1.0.4/json) | 1.0.4 | MIT |
| [yarl](https://pypi.org/pypi/yarl/1.25.1/json) | 1.25.1 | Apache-2.0 |
| [zopfli](https://pypi.org/pypi/zopfli/0.4.3/json) | 0.4.3 | Apache-2.0 |

## Imagem Docker e bibliotecas nativas

O Dockerfile preserva os arquivos de licença dos pacotes Python e do Debian em `/usr/share/doc`;
o `apt` só remove os índices de download. A base Python/Debian, Pango, HarfBuzz, GLib, FreeType,
fontconfig e as bibliotecas nativas incorporadas a wheels têm licenças próprias. `psycopg-binary`
pode incorporar libpq e bibliotecas criptográficas; veja a [documentação oficial de instalação](https://www.psycopg.org/psycopg3/docs/basic/install.html#binary-installation).

As versões dos pacotes `apt` e das bibliotecas internas dos wheels dependem da plataforma e do
artefato efetivamente construído. Este aviso não certifica a imagem inteira como MIT nem conclui
sozinho as obrigações de redistribuição de binários LGPL/MPL. Antes de publicar uma imagem,
registre seu digest e inventário efetivo, preserve avisos e licenças e disponibilize o código-fonte
correspondente das partes que o exigirem, incluindo alterações e condições de religação quando
aplicáveis. Não remova `*.dist-info/licenses`, avisos de dicionários ou `/usr/share/doc` para
diminuir a imagem. A revisão desse artefato é separada da licença do código próprio.
