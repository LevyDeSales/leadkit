"""Contrato estrutural online por revisão; somente catálogos, nunca dados da aplicação.

A assinatura é congelada em Python para acompanhar o wheel sem novos package-data.
Revisões futuras precisam de contrato próprio revisado, no mesmo artefato da migration.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import sqlalchemy as sa

# Nome/definição normalizados pelo próprio PostgreSQL 16, sem OIDs, owners ou ACLs locais.
CATALOG_QUERIES = {
    "namespace_object": """
        SELECT i.type || ':' || i.identity, ARRAY[i.type]
        FROM pg_depend d
        CROSS JOIN LATERAL pg_identify_object(d.classid,d.objid,d.objsubid) i
        WHERE d.refclassid='pg_namespace'::regclass
          AND d.refobjid='public'::regnamespace AND d.deptype='n' ORDER BY 1
    """,
    "schema": """
        SELECT nspname, ARRAY[]::text[] FROM pg_namespace
        WHERE nspname <> 'public' AND nspname <> 'information_schema'
          AND nspname !~ '^pg_' ORDER BY 1
    """,
    "relation": """
        SELECT n.nspname || '.' || c.relname,
               ARRAY[c.relkind::text, c.relpersistence::text, c.relrowsecurity::text,
                     c.relforcerowsecurity::text, c.relreplident::text,
                     coalesce(c.reloptions::text, ''),
                     coalesce(pg_get_expr(c.relpartbound, c.oid), '')]
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind NOT IN ('i', 'I', 'c') ORDER BY 1
    """,
    "column": """
        SELECT n.nspname || '.' || c.relname || '.' || a.attname,
               ARRAY[format_type(a.atttypid,a.atttypmod), a.attnotnull::text,
                     coalesce(pg_get_expr(d.adbin,d.adrelid), ''),
                     a.attidentity::text, a.attgenerated::text,
                     coalesce(cn.nspname || '.' || coll.collname, '')]
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        LEFT JOIN pg_collation coll ON coll.oid=a.attcollation
        LEFT JOIN pg_namespace cn ON cn.oid=coll.collnamespace
        WHERE n.nspname='public' AND c.relkind NOT IN ('i','I','c')
          AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1
    """,
    "constraint": """
        SELECT n.nspname || '.' || c.relname || '.' || con.conname,
               ARRAY[pg_get_constraintdef(con.oid, false), con.convalidated::text,
                     con.condeferrable::text, con.condeferred::text]
        FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY 1
    """,
    "index": """
        SELECT n.nspname || '.' || c.relname,
               ARRAY[pg_get_indexdef(i.indexrelid), i.indisvalid::text,
                     i.indisready::text, i.indisclustered::text, i.indisreplident::text]
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY 1
    """,
    "function": """
        SELECT n.nspname || '.' || p.proname || '(' ||
               pg_get_function_identity_arguments(p.oid) || ')',
               ARRAY[CASE WHEN p.prokind='a' THEN 'aggregate' ELSE pg_get_functiondef(p.oid) END]
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' ORDER BY 1
    """,
    "trigger": """
        SELECT n.nspname || '.' || c.relname || '.' || t.tgname,
               ARRAY[pg_get_triggerdef(t.oid, false), t.tgenabled::text]
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY 1
    """,
    # O nome RI_ConstraintTrigger_* incorpora um OID e muda no restore. A identidade portátil
    # vem da constraint/tabelas/função; estado, evento e deferral são propriedades independentes.
    "foreign_key_trigger": """
        SELECT n.nspname || '.' || c.relname || '.' ||
               cn.nspname || '.' || cc.relname || '.' || con.conname || '.' ||
               pn.nspname || '.' || p.proname,
               ARRAY[t.tgenabled::text, t.tgtype::text, t.tgdeferrable::text,
                     t.tginitdeferred::text,
                     pn.nspname || '.' || p.proname || '(' ||
                         pg_get_function_identity_arguments(p.oid) || ')',
                     rn.nspname || '.' || rc.relname,
                     coalesce(insp.nspname || '.' || ic.relname, ''),
                     t.tgnargs::text, encode(t.tgargs, 'hex'),
                     coalesce(pg_get_expr(t.tgqual,t.tgrelid), '')]
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_constraint con ON con.oid=t.tgconstraint AND con.contype='f'
        JOIN pg_class cc ON cc.oid=con.conrelid
        JOIN pg_namespace cn ON cn.oid=cc.relnamespace
        JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        JOIN pg_class rc ON rc.oid=t.tgconstrrelid
        JOIN pg_namespace rn ON rn.oid=rc.relnamespace
        LEFT JOIN pg_class ic ON ic.oid=t.tgconstrindid
        LEFT JOIN pg_namespace insp ON insp.oid=ic.relnamespace
        WHERE n.nspname='public' AND t.tgisinternal ORDER BY 1
    """,
    "rewrite_rule": """
        SELECT n.nspname || '.' || c.relname || '.' || r.rulename,
               ARRAY[pg_get_ruledef(r.oid, false), r.ev_enabled::text]
        FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY 1
    """,
    "policy": """
        SELECT n.nspname || '.' || c.relname || '.' || p.polname,
               ARRAY[p.polcmd::text, p.polpermissive::text, p.polroles::text,
                     coalesce(pg_get_expr(p.polqual,p.polrelid),''),
                     coalesce(pg_get_expr(p.polwithcheck,p.polrelid),'')]
        FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY 1
    """,
    "view": """
        SELECT n.nspname || '.' || c.relname, ARRAY[pg_get_viewdef(c.oid,false)]
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('v','m') ORDER BY 1
    """,
    "type": """
        SELECT n.nspname || '.' || t.typname,
               ARRAY[t.typtype::text, format_type(t.typbasetype,t.typtypmod),
                     t.typnotnull::text, coalesce(t.typdefault,''),
                     coalesce((SELECT string_agg(e.enumlabel,',' ORDER BY e.enumsortorder)
                               FROM pg_enum e WHERE e.enumtypid=t.oid),'')]
        FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname='public' AND t.typrelid=0 AND t.typelem=0 ORDER BY 1
    """,
    "sequence": """
        SELECT schemaname || '.' || sequencename,
               ARRAY[data_type::text, start_value::text, min_value::text, max_value::text,
                     increment_by::text, cycle::text, cache_size::text]
        FROM pg_sequences WHERE schemaname='public' ORDER BY 1
    """,
    "extension": """
        SELECT extname, ARRAY[extversion,n.nspname]
        FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
        WHERE extname <> 'plpgsql' ORDER BY 1
    """,
    "event_trigger": """
        SELECT evtname, ARRAY[evtevent,evtenabled::text,evtfoid::regprocedure::text,
                             coalesce(evttags::text,'')]
        FROM pg_event_trigger ORDER BY 1
    """,
}


# pg_dump/restauração reanalisa varchar[]::text[] como casts individuais de cada literal.
# Normalizamos SÓ esse caso: mesmos literais, mesma ordem e mesmos tipos; nenhum SQL arbitrário.
_VARCHAR_LITERAL = r"'(?:[^']|'')*'::character varying"
_TEXT_ARRAY_CAST = re.compile(
    r"\(\(ARRAY\[(" + _VARCHAR_LITERAL + r"(?:, " + _VARCHAR_LITERAL + r")*)\]\)::text\[\]\)"
)
_LITERAL = re.compile(_VARCHAR_LITERAL)


def canonical_sql(definition: str) -> str:
    """Equivalência estrita do cast textual que o pg_dump 16 reescreve no restore."""
    return _TEXT_ARRAY_CAST.sub(
        lambda match: (
            "(ARRAY["
            + ", ".join(f"({literal})::text" for literal in _LITERAL.findall(match.group(1)))
            + "])"
        ),
        definition,
    )


def schema_signature(connection: sa.Connection) -> dict[str, list[str]]:
    """Estrutura portátil em public; objetos extras são diferenças, não ignorados."""
    result = {}
    for kind, query in CATALOG_QUERIES.items():
        for name, properties in connection.exec_driver_sql(query):
            result[f"{kind}:{name}"] = (
                [canonical_sql(properties[0]), *properties[1:]]
                if kind in {"constraint", "index"}
                else properties
            )
    return result


def load_contracts(directory: Path) -> dict[str, dict]:
    """Carrega contratos versionados, sem aceitar revisão/assinatura do ambiente."""
    spec = importlib.util.spec_from_file_location(
        "leadkit_schema_contracts", directory / "contracts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SIGNATURES


def validate_database(connection: sa.Connection, contracts: dict[str, dict]) -> str | None:
    """Recusa banco incompatível antes do Alembic criar até a tabela de versão."""
    if connection.dialect.name != "postgresql" or connection.dialect.server_version_info[0] != 16:
        raise RuntimeError("a ponte exige PostgreSQL 16 e verificação online")
    signature = schema_signature(connection)
    if "relation:public.alembic_version" not in signature:
        if signature:
            raise RuntimeError("banco ocupado sem marcador; nenhuma escrita foi autorizada")
        return None
    try:
        revisions = (
            connection.exec_driver_sql(
                "SELECT version_num FROM public.alembic_version ORDER BY version_num"
            )
            .scalars()
            .all()
        )
    except sa.exc.DBAPIError:
        raise RuntimeError("revisão incompatível; marcador Alembic inválido") from None
    if len(revisions) != 1 or revisions[0] not in contracts:
        raise RuntimeError(
            "revisão incompatível; use a cadeia original até a fronteira suportada, sem stamp"
        )
    revision = revisions[0]
    expected = contracts[revision]
    differences = sorted(
        name
        for name in signature.keys() | expected.keys()
        if signature.get(name) != expected.get(name)
    )
    if differences:
        # Só nomes de objetos: nunca valores, DDL de função, ciphertexts ou DSN.
        raise RuntimeError("estrutura incompatível; objetos: " + ", ".join(differences[:12]))
    return revision
