"""O catálogo de ferramentas: uma por operação do `openapi.json`, gerada, e não escrita à mão.

`load` lê o documento (o da API viva, ou o `docs/api/openapi.json` nos testes) e devolve uma
`ToolSpec` por `operationId`: o nome é o `operation_id`; a descrição, o `summary` e a `description` da
operação (em português, escritos para quem nunca viu o sistema); o esquema de entrada junta os
parâmetros do caminho e da query com os campos do corpo, num objeto só; e as marcas viram dicas
(GET/HEAD sem mutação e operações de papel leitura são `readOnlyHint`; `x-destructive` é
`destructiveHint` e
`x-external-effect` é `openWorldHint`). `select` aplica, na subida, o papel da chave e a opção do dado
da lead.

Não depende do SDK do MCP nem do httpx: é uma função do documento para dados, testável sem rede.

**Cobertura de 100%.** `EXCEPTIONS` é a lista das operações que ficam de fora do catálogo de propósito,
cada uma com o motivo. Começa **vazia**, e o teste de cobertura (`tests_app/test_admin_mcp_catalog.py`)
cobra que o catálogo gerado de `docs/api/openapi.json` tenha exatamente os `operation_id` do arquivo
(menos as exceções). Uma operação que o gerador não sabe montar não é calada: `load` a devolve em
`Catalog.skipped`, com o motivo, e o servidor a diz no log, para a falta aparecer; no teste, `skipped`
tem de ser vazio.

**Dado da lead.** O catálogo tem as operações `x-lead-data` inteiras. Sem `GERADOR_MCP_LEAD_DATA=1` (e
sem uma chave `total`), `select` tira da ferramenta o argumento `include_lead_data` (o servidor o fixa em
falso) e o `q` do `list_jobs`, que com a chave `total` busca por parte do nome ou do e-mail da lead e
serviria para sondar quem é lead; e deixa de fora a operação cuja resposta inteira é dado da lead
(`download_job_pdf`, que não tem `include_lead_data`).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

# Operações que ficam fora do catálogo de propósito: `operation_id` -> o motivo. **Começa vazia**: uma
# exceção exige uma edição deliberada aqui, com o motivo, e a revisão a vê.
EXCEPTIONS: Mapping[str, str] = {}

ROLES = ("leitura", "total")
HTTP_METHODS = ("get", "put", "post", "delete", "patch")
REF_PREFIX = "#/components/schemas/"
DEFS_PREFIX = "#/$defs/"

# O argumento que liga o dado da lead nas operações que o têm, e os que sondam quem é lead (por
# operação). Sem `GERADOR_MCP_LEAD_DATA=1`, somem da ferramenta.
LEAD_FLAG = "include_lead_data"
LEAD_PROBES: Mapping[str, tuple[str, ...]] = {"list_jobs": ("q",)}

CONFIRM = "confirm"
CONFIRM_PREFIX = "confirm_"

# O que o `confirm` diz no esquema da ferramenta (no da API ele diz como se chama a exigência; aqui, o
# que o servidor MCP faz com ele).
CONFIRM_TEXT = (
    "Só para quando o cliente MCP não sabe perguntar à pessoa: sem essa pergunta, a primeira chamada "
    "só devolve a descrição do efeito, e a operação executa na chamada seguinte, com `confirm: true`, "
    "depois que a pessoa disser que sim. Quando o cliente sabe perguntar, o servidor pergunta sozinho "
    "e ignora este campo."
)
FILE_TEXT = (
    "No MCP, informe o caminho de uma imagem dentro da pasta `GERADOR_MCP_UPLOAD_DIR` do computador de "
    "quem roda o servidor (um caminho sem `/` no começo vale a partir dela): ele lê o arquivo e o envia, "
    "depois de a pessoa confirmar."
)
UPLOAD_TEXT = (
    "envia uma imagem do computador de quem roda o servidor, e a imagem enviada passa a ser servida "
    "sem login (`/img/<sha256>`)"
)

# O que o servidor controla (a API que ele lê) não escolhe o que o MCP faz com o texto: o `operation_id`
# vira o nome da ferramenta e do arquivo salvo, o caminho vira parte da URL, e o nome de um argumento
# vira chave do esquema. Fora destes formatos, a operação não vira ferramenta (`Catalog.skipped`).
OPERATION_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PATH_TEMPLATE = re.compile(r"^(/[A-Za-z0-9_-]+|/\{[a-z_]+\})+$")
ARGUMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TEMPLATE_ARGUMENT = re.compile(r"\{([a-z_]+)\}")

# Não entram no esquema da ferramenta: só gastam o contexto do modelo.
NOISE = frozenset({"title"})
# Palavras cujo valor é dado, e não esquema: o `title` de um valor de `default` ou de `enum` fica.
VALUE_KEYWORDS = frozenset({"default", "examples", "example", "const", "enum"})
# Palavras cujo valor é um mapa nome -> esquema: as chaves são nomes, e não palavras do esquema.
SCHEMA_MAPS = frozenset({"properties", "$defs", "patternProperties", "dependentSchemas"})

DESTRUCTIVE_TEXT = "apaga ou sobrescreve algo difícil de desfazer"
EXTERNAL_TEXT = (
    "muda o que a lead vê, para onde os dados dela vão, chama um serviço de fora (às vezes pago) "
    "ou cria uma chave de API"
)


class CatalogError(Exception):
    """O gerador não sabe montar a ferramenta de uma operação. A mensagem diz o que não entendeu."""


class Fixed(NamedTuple):
    """Um argumento que o servidor fixa e que a ferramenta não mostra: onde ele vai e o valor."""

    where: str
    value: Any


@dataclass(frozen=True)
class ToolSpec:
    """Uma ferramenta do MCP, gerada de uma operação da API.

    `locations` diz onde cada argumento vai na requisição: `path`, `query`, `body` (campo do corpo
    JSON ou texto de um formulário `multipart`), `file` (o caminho de uma imagem que o servidor lê e
    envia) ou `local` (o `confirm` de uma ferramenta de arquivo, que é só do MCP e não vai à API).
    `fixed` são os argumentos que o servidor fixa e que a ferramenta não mostra (o
    `include_lead_data=false`).
    """

    name: str
    method: str
    path: str
    summary: str
    details: str
    role: str
    destructive: bool
    external_effect: bool
    lead_data: bool
    confirm_unless: str | None
    input_schema: Mapping[str, Any]
    locations: Mapping[str, str]
    body: str | None
    body_required: bool
    returns: tuple[str, ...]
    companions: tuple[str, ...]
    notes: tuple[str, ...] = ()
    fixed: Mapping[str, Fixed] = field(default_factory=dict)

    @property
    def marked(self) -> bool:
        """Tem efeito difícil de desfazer ou para fora do sistema: pede confirmação."""
        return self.destructive or self.external_effect

    @property
    def has_file(self) -> bool:
        """Recebe um arquivo do computador de quem roda o servidor: pede confirmação sempre, marcada
        ou não, porque o modelo escolhe o caminho."""
        return "file" in self.locations.values()

    @property
    def read_only(self) -> bool:
        return not self.destructive and (self.method in ("GET", "HEAD") or self.role == "leitura")

    @property
    def description(self) -> str:
        parts = [self.summary]
        if self.details:
            parts.append(self.details)
        parts.extend(self.notes)
        return "\n\n".join(parts)

    @property
    def annotations(self) -> dict[str, bool]:
        """As dicas do MCP, sempre as três e explícitas: o padrão do protocolo para o que falta é
        «destrutiva» e «mundo aberto», o contrário do que uma consulta quer dizer."""
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "openWorldHint": self.external_effect,
        }

    @property
    def effects(self) -> list[str]:
        """O efeito da operação em palavras, para a pergunta à pessoa."""
        found = []
        if self.destructive:
            found.append(DESTRUCTIVE_TEXT)
        if self.external_effect:
            found.append(EXTERNAL_TEXT)
        if self.has_file:
            found.append(UPLOAD_TEXT)
        return found

    @property
    def non_json(self) -> bool:
        """A resposta de sucesso não é JSON (PDF ou HTML): o servidor a salva num arquivo."""
        return bool(self.returns) and "application/json" not in self.returns

    def needs_confirmation(self, arguments: Mapping[str, Any]) -> bool:
        """Marcada, e sem a dispensa da simulação (`x-confirm-unless`: `dry_run: true`); ou com arquivo,
        que não tem dispensa."""
        if self.has_file:
            return True
        if not self.marked:
            return False
        return not (self.confirm_unless and arguments.get(self.confirm_unless) is True)


@dataclass(frozen=True)
class Catalog:
    """O que `load` achou: as ferramentas por nome e, em `skipped`, o que não soube montar."""

    tools: Mapping[str, ToolSpec]
    skipped: Mapping[str, str]


def load(document: Mapping[str, Any], *, exceptions: Mapping[str, str] = EXCEPTIONS) -> Catalog:
    """Gera uma `ToolSpec` por operação do documento, menos as `exceptions`.

    Uma operação que não sabe montar vai para `skipped` com o motivo; as outras seguem. O documento
    inteiro inválido (sem `paths`) é `CatalogError`.
    """
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise CatalogError("O documento não tem `paths`: não é o openapi.json da API de admin.")
    # O documento vem de fora: `components` e `components.schemas` ausentes valem como vazios, mas um
    # valor que não é objeto é um documento que não é o da API (e não um `AttributeError` na subida).
    holder = document.get("components", {})
    if not isinstance(holder, Mapping) or not isinstance(holder.get("schemas", {}), Mapping):
        raise CatalogError(
            "O documento tem `components` que não é um objeto: não é o da API de admin."
        )
    components = holder.get("schemas", {})
    tools: dict[str, ToolSpec] = {}
    skipped: dict[str, str] = {}
    for path, item in sorted(paths.items(), key=lambda pair: str(pair[0])):
        if not isinstance(item, Mapping):
            skipped[str(path)] = "o caminho não descreve operações"
            continue
        shared = item.get("parameters", [])
        for method in HTTP_METHODS:
            operation = item.get(method)
            if operation is None:
                continue
            if not isinstance(operation, Mapping):
                skipped[f"{method.upper()} {path}"] = "a operação não é um objeto"
                continue
            name = operation.get("operationId")
            label = name if _is_name(name) else f"{method.upper()} {path}"
            if isinstance(name, str) and name in exceptions:
                continue
            try:
                if not name:
                    raise CatalogError("a operação não tem `operationId`")
                if not _is_name(name):
                    raise CatalogError("`operationId` fora de `[a-z][a-z0-9_]{0,63}`")
                if not isinstance(path, str) or not PATH_TEMPLATE.fullmatch(path):
                    raise CatalogError("caminho fora de `/segmento` e `/{argumento}`")
                if name in tools:
                    raise CatalogError("`operationId` repetido")
                tools[name] = _tool(
                    name, method, path, operation, shared=shared, components=components
                )
            except CatalogError as problem:
                skipped[label] = str(problem)
            except (AttributeError, KeyError, TypeError, ValueError) as problem:
                # O documento vem de fora: um formato que o gerador nem consegue ler é uma operação
                # que não vira ferramenta, e não uma subida que cai com a pilha na tela.
                skipped[label] = f"formato inesperado ({type(problem).__name__})"
    return Catalog(tools=dict(sorted(tools.items())), skipped=skipped)


def select(tools: Iterable[ToolSpec], *, role: str, lead_data: bool) -> list[ToolSpec]:
    """As ferramentas que a chave de papel `role` pode usar, com a política do dado da lead.

    `lead_data` é a opção `GERADOR_MCP_LEAD_DATA=1`. O dado da lead só aparece com a opção **e** com
    uma chave `total` (com `leitura`, a API responde 403 a `include_lead_data`).
    """
    if role not in ROLES:
        raise CatalogError(f"Papel desconhecido: {role!r}.")
    show_lead_data = lead_data and role == "total"
    chosen = []
    for tool in tools:
        if tool.role == "total" and role != "total":
            continue
        if tool.lead_data and not show_lead_data:
            if LEAD_FLAG not in tool.locations:
                continue  # a resposta inteira é dado da lead: sem a opção, a ferramenta não existe
            tool = _without_lead_data(tool)
        chosen.append(tool)
    return chosen


# --- da operação à ferramenta --------------------------------------------------------------------


def _is_name(name: Any) -> bool:
    return isinstance(name, str) and OPERATION_ID.fullmatch(name) is not None


def _tool(
    name: str,
    method: str,
    path: str,
    operation: Mapping[str, Any],
    *,
    shared: list[Any],
    components: Mapping[str, Any],
) -> ToolSpec:
    role = operation.get("x-required-role")
    if role not in ROLES:
        raise CatalogError(f"`x-required-role` inválido: {role!r}")
    properties: dict[str, Any] = {}
    required: list[str] = []
    locations: dict[str, str] = {}

    def add(prop: str, schema: Any, where: str, is_required: bool) -> None:
        if not isinstance(prop, str) or not ARGUMENT_NAME.fullmatch(prop):
            raise CatalogError("o nome de um argumento fora de `[A-Za-z_][A-Za-z0-9_]{0,63}`")
        if prop in properties:
            raise CatalogError(f"o argumento `{prop}` aparece duas vezes (caminho, query ou corpo)")
        properties[prop] = schema
        locations[prop] = where
        if is_required:
            required.append(prop)

    for raw in [*shared, *operation.get("parameters", [])]:
        if "$ref" in raw:
            raise CatalogError("parâmetro por `$ref`: o gerador não o resolve")
        where = raw.get("in")
        if where not in ("path", "query"):
            raise CatalogError(f"parâmetro {raw.get('name')!r} em {where!r}: só caminho e query")
        schema = dict(raw.get("schema") or {})
        if raw.get("description") and "description" not in schema:
            schema["description"] = raw["description"]
        add(raw["name"], schema, where, bool(raw.get("required")) or where == "path")

    body_kind: str | None = None
    body_required = False
    request_body = operation.get("requestBody")
    if request_body:
        body_kind, body_required = _add_body(request_body, components, add)

    in_path = {prop for prop, where in locations.items() if where == "path"}
    if set(_TEMPLATE_ARGUMENT.findall(path)) != in_path:
        raise CatalogError(
            "os `{argumentos}` do caminho não são os parâmetros de caminho declarados"
        )

    marked = bool(operation.get("x-destructive") or operation.get("x-external-effect"))
    has_file = "file" in locations.values()
    if marked:
        if CONFIRM not in properties:
            raise CatalogError("operação marcada sem o campo `confirm`")
        properties[CONFIRM] = {"type": "boolean", "description": CONFIRM_TEXT}
        if CONFIRM in required:
            required.remove(CONFIRM)
    elif has_file:
        # Um arquivo do computador de quem roda o servidor pede a confirmação da pessoa mesmo sem marca
        # da API; o `confirm` é só do MCP (`local`), e a API não o vê.
        if CONFIRM in properties:
            raise CatalogError("operação com arquivo que já tem um campo `confirm` que não é marca")
        properties[CONFIRM] = {"type": "boolean", "description": CONFIRM_TEXT}
        locations[CONFIRM] = "local"

    confirm_unless = operation.get("x-confirm-unless")
    if confirm_unless is not None:
        field_schema = properties.get(confirm_unless)
        if not marked or locations.get(confirm_unless) != "body" or not _is_boolean(field_schema):
            raise CatalogError("`x-confirm-unless` não aponta um booleano do corpo de uma marcada")

    for prop, where in list(locations.items()):
        if where == "file":
            description = properties[prop].get("description", "")
            properties[prop] = {
                "type": "string",
                "description": f"{description} {FILE_TEXT}".strip(),
            }

    schema = _input_schema(properties, required, components)
    companions = tuple(
        prop
        for prop, sub in properties.items()
        if prop.startswith(CONFIRM_PREFIX) and _is_boolean(sub)
    )
    notes = _notes(operation, marked, confirm_unless, has_file)
    return ToolSpec(
        name=name,
        method=method.upper(),
        path=path,
        summary=str(operation.get("summary") or name).strip(),
        details=str(operation.get("description") or "").strip(),
        role=role,
        destructive=bool(operation.get("x-destructive")),
        external_effect=bool(operation.get("x-external-effect")),
        lead_data=bool(operation.get("x-lead-data")),
        confirm_unless=confirm_unless,
        input_schema=schema,
        locations=locations,
        body=body_kind,
        body_required=body_required,
        returns=_success_types(operation),
        companions=companions,
        notes=notes,
    )


def _add_body(
    request_body: Mapping[str, Any], components: Mapping[str, Any], add: Any
) -> tuple[str, bool]:
    content = request_body.get("content") or {}
    if "application/json" in content:
        kind = "json"
    elif "multipart/form-data" in content:
        kind = "multipart"
    else:
        raise CatalogError(f"corpo {list(content) or 'sem tipo'}: só JSON e multipart")
    media = "application/json" if kind == "json" else "multipart/form-data"
    ref = (content[media].get("schema") or {}).get("$ref", "")
    if not ref.startswith(REF_PREFIX) or ref[len(REF_PREFIX) :] not in components:
        raise CatalogError("o corpo não é um modelo com nome (`$ref` de um schema)")
    model = components[ref[len(REF_PREFIX) :]]
    if model.get("type") != "object" or not isinstance(model.get("properties"), Mapping):
        raise CatalogError("o corpo não é um objeto com propriedades")
    wanted = bool(request_body.get("required"))
    needed = set(model.get("required") or ())
    for prop, sub in model["properties"].items():
        where = "file" if _is_file(sub) else "body"
        add(prop, copy.deepcopy(sub), where, wanted and prop in needed)
    return kind, wanted


def _is_file(schema: Mapping[str, Any]) -> bool:
    return "contentMediaType" in schema or schema.get("format") == "binary"


def _is_boolean(schema: Any) -> bool:
    return isinstance(schema, Mapping) and schema.get("type") == "boolean"


def _success_types(operation: Mapping[str, Any]) -> tuple[str, ...]:
    for status, response in operation.get("responses", {}).items():
        if str(status).startswith("2"):
            return tuple((response.get("content") or {}).keys())
    return ()


def _notes(
    operation: Mapping[str, Any],
    marked: bool,
    confirm_unless: str | None,
    has_file: bool,
) -> tuple[str, ...]:
    notes = []
    if marked:
        text = (
            "Nota do servidor MCP: esta operação tem efeito difícil de desfazer ou para fora do "
            "sistema, então o servidor pede a confirmação da pessoa antes de executar (pelo "
            "cliente, se ele sabe perguntar; senão, a primeira chamada só devolve a descrição e a "
            "operação executa na chamada seguinte, com `confirm: true`)."
        )
        if confirm_unless:
            text += f" Com `{confirm_unless}: true` (só simula) ele não pergunta."
        notes.append(text)
    if has_file:
        notes.append(
            "Nota do servidor MCP: o arquivo vai pelo caminho, e só se estiver dentro da pasta "
            "`GERADOR_MCP_UPLOAD_DIR` (sem ela definida, a ferramenta recusa). O servidor sempre pede a "
            "confirmação da pessoa, mostrando o caminho e o tamanho, porque a imagem enviada passa a ser "
            "servida sem login; só imagem PNG, JPEG ou WebP, e qualquer outro arquivo o servidor recusa "
            "sem enviar."
        )
    if any(
        not media.startswith("application/json")
        for status, response in operation.get("responses", {}).items()
        if str(status).startswith("2")
        for media in (response.get("content") or {})
    ):
        notes.append(
            "Nota do servidor MCP: a resposta não é JSON. O servidor a salva num arquivo "
            "(só do usuário) e devolve o caminho e o tamanho, sem o conteúdo."
        )
    return tuple(notes)


# --- o esquema de entrada ------------------------------------------------------------------------


def _input_schema(
    properties: dict[str, Any], required: list[str], components: Mapping[str, Any]
) -> dict[str, Any]:
    definitions: dict[str, Any] = {}

    def collect(node: Any) -> Any:
        """Troca cada `$ref` de `components/schemas` por um de `$defs` e junta o fecho dos modelos."""
        if isinstance(node, list):
            return [collect(item) for item in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                if not value.startswith(REF_PREFIX) or value[len(REF_PREFIX) :] not in components:
                    raise CatalogError(f"`$ref` que o gerador não resolve: {value}")
                target = value[len(REF_PREFIX) :]
                if target not in definitions:
                    definitions[
                        target
                    ] = {}  # reserva o lugar antes de descer: o modelo pode se citar
                    definitions[target] = collect(_tidy(components[target]))
                out[key] = DEFS_PREFIX + target
            elif key in VALUE_KEYWORDS:
                out[key] = value
            elif key in SCHEMA_MAPS and isinstance(value, dict):
                out[key] = {name: collect(sub) for name, sub in value.items()}
            else:
                out[key] = collect(value)
        return out

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {prop: collect(_tidy(sub)) for prop, sub in properties.items()},
        "required": required,
        "additionalProperties": False,
    }
    if definitions:
        schema["$defs"] = dict(sorted(definitions.items()))
    return schema


def _tidy(node: Any) -> Any:
    """Uma cópia do esquema sem o que só gasta o contexto do modelo (o `title`)."""
    if isinstance(node, list):
        return [_tidy(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in NOISE:
            continue
        if key in VALUE_KEYWORDS:
            out[key] = value
        elif key in SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _tidy(sub) for name, sub in value.items()}
        else:
            out[key] = _tidy(value)
    return out


# --- a política do dado da lead ------------------------------------------------------------------


def _without_lead_data(tool: ToolSpec) -> ToolSpec:
    """A ferramenta sem o dado da lead: `include_lead_data` fixo em falso e sem os argumentos que
    sondam quem é lead."""
    hidden = {LEAD_FLAG, *LEAD_PROBES.get(tool.name, ())}
    schema = copy.deepcopy(dict(tool.input_schema))
    schema["properties"] = {k: v for k, v in schema["properties"].items() if k not in hidden}
    schema["required"] = [name for name in schema["required"] if name not in hidden]
    note = (
        "Nota do servidor MCP: os dados da lead estão desligados (`GERADOR_MCP_LEAD_DATA`): "
        f"`{LEAD_FLAG}` fica sempre falso, e a resposta não traz nome, e-mail nem respostas da lead."
    )
    probes = LEAD_PROBES.get(tool.name, ())
    if probes:
        note += (
            f" Por isso o argumento `{probes[0]}` não existe aqui (buscar por nome ou e-mail "
            "serviria para sondar quem é lead); para achar um pedido pelo código, use `get_job`."
        )
    return replace(
        tool,
        input_schema=schema,
        locations={k: v for k, v in tool.locations.items() if k not in hidden},
        fixed={**tool.fixed, LEAD_FLAG: Fixed(tool.locations[LEAD_FLAG], False)},
        notes=(*tool.notes, note),
    )
