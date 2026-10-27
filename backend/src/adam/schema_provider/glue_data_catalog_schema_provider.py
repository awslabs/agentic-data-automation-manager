#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Glue Data Catalog implementation of SchemaProvider.

The index is one Glue database the stack owns (SCHEMA_INDEX_DATABASE). Each
scanned table becomes one Glue table in it, and its DDL -- read from the source
database by that engine's `scan_schema` -- is published as the asset's business
context, which is what Glue's semantic SearchAssets matches against.

Every call uses this Lambda's own role. There is no per-caller Glue access:
which tables a caller may be shown is decided by the database and applied by the
base class.

Every Glue table a publish creates carries OWNER_PARAMETER = the deployment's
stack name, beside the source database and engine it was scanned from. Only
tables carrying it are ever deleted: a table someone else put in the index is
updated if a scan names it, but is never marked and never removed.

Must not be VPC-attached on the search side: the read Lambda needs Glue over the
public endpoint, and an external VPC may have no NAT and no Glue endpoint.
"""

import hashlib
import logging
import re
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3

from adam.schema_provider.schema_provider import (
    SchemaMatch,
    SchemaProvider,
    VisibleObject,
)
from adam.shared.environment import require_env
from adam.shared.schema_types import SchemaObject

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ── Source identity ─────────────────────────────────────────────────────

# Glue classification <-> the engine name the connection registry routes on.
_ENGINE_FROM_CLASSIFICATION = {
    "postgresql": "postgres",
    "oracle": "oracle",
    "mysql": "mysql",
    "mariadb": "mariadb",
    "redshift": "redshift",
}
_CLASSIFICATION_FROM_ENGINE = {v: k for k, v in _ENGINE_FROM_CLASSIFICATION.items()}


class SourceIdentityError(RuntimeError):
    """A catalogued table cannot be traced back to a queryable object."""


def _split_location(location: str) -> list[str]:
    """Split a dotted source path, keeping quoted segments whole.

    Oracle service names contain dots, so segments are quoted:
    `"salesdb_SALESPDB.paas.oracle.com".MCP_DEV.CLIENTS`.
    """
    parts: list[str] = []
    current = ""
    in_quotes = False
    for char in location:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "." and not in_quotes:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [p for p in parts if p]


def _source_identity(table: dict) -> dict[str, str]:
    """The real database, schema and table behind a Glue table.

    The Glue name exists in no database; `StorageDescriptor.Location` holds the
    real path, and its first segment is the database string `execute_statement`
    resolves to the caller's connection.
    """
    location = (table.get("StorageDescriptor") or {}).get("Location", "")
    segments = _split_location(location)
    if len(segments) < 2:
        raise SourceIdentityError(
            f"Glue table {table.get('Name')!r} has Location {location!r}, which "
            "does not name a database and table. This provider cannot tell the "
            "agent which connection to query."
        )

    classification = (table.get("Parameters") or {}).get("classification", "")
    engine = _ENGINE_FROM_CLASSIFICATION.get(classification.lower(), "")
    if not engine:
        raise SourceIdentityError(
            f"Glue table {table.get('Name')!r} has classification "
            f"{classification!r}, which maps to no engine. Add it to "
            "_ENGINE_FROM_CLASSIFICATION."
        )

    database, *middle, table_name = segments
    return {
        "database": database,
        "schema": middle[-1] if middle else "",
        "table": table_name,
        "engine": engine,
        # Schema-qualified: neither database puts these in the user's default schema.
        "qualified_name": f"{middle[-1]}.{table_name}" if middle else table_name,
    }


def _location(database: str, schema: str, table: str) -> str:
    """The inverse of `_split_location`, quoting every segment."""
    for part in (database, schema, table):
        if '"' in part:
            raise ValueError(f"Cannot catalogue an identifier containing a quote: {part!r}")
    return f'"{database}"."{schema}"."{table}"'


_NAME_UNSAFE = re.compile(r"[^a-z0-9_]+")

# Glue's limit on a table name.
_MAX_NAME_LENGTH = 255
_HASH_LENGTH = 12


def _catalog_name(engine: str, database: str, schema: str, table: str) -> str:
    """The Glue table name for one source table. Deterministic, so re-publishing replaces.

    `database` is the source's identity, which contains its host. The name carries a
    short hash of it instead: names stay a fixed length however long the host is,
    never expose a hostname, and two databases cannot collide. The readable
    identity is kept in the table's parameters, which is what search matches on.
    """
    where = hashlib.sha256(database.strip().lower().encode()).hexdigest()[:_HASH_LENGTH]
    name = "__".join(_NAME_UNSAFE.sub("_", part.lower()) for part in (engine, where, schema, table))
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(
            f"{schema}.{table} would be catalogued as a {len(name)}-character "
            f"name; Glue allows {_MAX_NAME_LENGTH}."
        )
    return name


def _query_terms(query: str) -> list[str]:
    """The query's distinct whitespace-separated terms, in order.

    SearchAssets recall drops as SearchText grows, so each term is also searched
    on its own. The whole query is searched separately, so a one-term query
    yields no extra terms.
    """
    terms = list(dict.fromkeys(query.split()))
    return [] if len(terms) == 1 else terms


def _asset_database_and_table(asset_id: str) -> tuple[str, str]:
    """Database and table from `arn:aws:glue:<region>:<acct>:table/<db>/<table>`."""
    _, marker, tail = asset_id.partition(":table/")
    parts = tail.split("/")
    if not marker or len(parts) != 2 or not all(parts):
        raise ValueError(f"Not a Glue table asset ARN: {asset_id!r}")
    return parts[0], parts[1]


# ── Provider implementation ─────────────────────────────────────────────

# Per search text. Ranking past this depth adds catalog reads, not relevance.
_SEARCH_DEPTH = 50

# How long a new table's asset may take to appear; see _describe_assets.
_ASSET_WAIT_SECONDS = 120
_ASSET_RETRY_SECONDS = 2


# Table parameter naming the deployment that created the table; see the module docstring.
OWNER_PARAMETER = "created_by_stack"


class GlueDataCatalogSchemaProvider(SchemaProvider):

    def __init__(self, *, index_database: str, region: str, catalog_id: str, owner: str,
                 glue_client=None, clock=time.monotonic, sleep=time.sleep):
        for name, value in (("index_database", index_database), ("region", region),
                            ("catalog_id", catalog_id), ("owner", owner)):
            if not value:
                raise ValueError(f"{name} is required.")
        self._index_database = index_database
        self._owner = owner
        self._region = region
        self._catalog_id = catalog_id
        self._glue = glue_client or boto3.client("glue")
        self._clock = clock
        self._sleep = sleep

    def _table_arn(self, name: str) -> str:
        return (f"arn:aws:glue:{self._region}:{self._catalog_id}:"
                f"table/{self._index_database}/{name}")

    # ── Read ────────────────────────────────────────────────────────────

    def _rank(self, query: str) -> Iterator[SchemaMatch]:
        # Glue tables only; the catalog also federates non-SQL assets.
        filter_clause = {"AndAllFilters": [
            {"AttributeFilter": {"Attribute": "type",
                                 "Operator": "equals",
                                 "Value": {"StringValue": "Table"}}},
            {"AttributeFilter": {"Attribute": "namespace",
                                 "Operator": "equals",
                                 "Value": {"StringValue": "amazon.glue"}}},
        ]}

        def _search(text: str) -> list[dict]:
            return self._glue.search_assets(
                SearchText=text, FilterClause=filter_clause, MaxResults=_SEARCH_DEPTH,
            ).get("Items", [])

        searches = [query, *_query_terms(query)]
        with ThreadPoolExecutor(max_workers=min(len(searches), 8)) as pool:
            responses = list(pool.map(_search, searches))

        # Union, keeping each asset at its best rank; earlier searches win ties.
        best: dict[str, tuple[int, int]] = {}
        described: dict[str, str] = {}
        for search_index, items in enumerate(responses):
            for rank, item in enumerate(items):
                asset_id = item["Id"]
                if item.get("AssetDescription"):
                    described[asset_id] = item["AssetDescription"]
                score = (rank, search_index)
                if asset_id not in best or score < best[asset_id]:
                    best[asset_id] = score
        ordered_ids = sorted(best, key=lambda i: best[i])
        logger.info(
            "SearchAssets: %d search(es) over %r yielded %d distinct candidate(s)",
            len(searches), query[:80], len(ordered_ids),
        )

        for asset_id in ordered_ids:
            db_name, table_name = _asset_database_and_table(asset_id)
            # The account's catalog holds other people's tables too.
            if db_name != self._index_database:
                continue
            ddl = described.get(asset_id, "")
            if not ddl:
                logger.error("%s.%s has no published DDL; republish its database.",
                             db_name, table_name)
                continue
            try:
                table = self._glue.get_table(DatabaseName=db_name, Name=table_name)["Table"]
            except self._glue.exceptions.EntityNotFoundException:
                # Removed by a publish between the search and this read.
                continue
            try:
                source = _source_identity(table)
            except SourceIdentityError as e:
                logger.error("Dropping %s.%s: %s", db_name, table_name, e)
                continue
            yield SchemaMatch(data_source=source["database"], engine=source["engine"],
                              schema=source["schema"], name=source["table"], definition=ddl)

    # ── Write ───────────────────────────────────────────────────────────

    def _published(self, database: str, engine: str) -> dict[str, dict[str, str]]:
        """Name -> parameters of every index table recorded as scanned from this database."""
        tables = {}
        for page in self._glue.get_paginator("get_tables").paginate(
                DatabaseName=self._index_database):
            for table in page.get("TableList", []):
                params = table.get("Parameters") or {}
                if params.get("source_database") == database and params.get("source_engine") == engine:
                    tables[table["Name"]] = params
        return tables

    def _ours(self, params: dict[str, str]) -> bool:
        return params.get(OWNER_PARAMETER) == self._owner

    def _describe_assets(self, descriptions: dict[str, str]) -> None:
        """Write each asset's business context, waiting for new assets to exist.

        Glue registers a table's asset asynchronously after CreateTable, so an
        immediate UpdateAsset raises EntityNotFound. Measured on a fresh index
        database: the asset appeared 1.2 s, 5.5 s and 6.7 s after its table.
        Tables are all written first so most assets exist by the time they are
        reached; any that still do not are retried until the deadline. So is an
        asset Glue refuses as being changed concurrently: two databases indexed
        at once, into the same index database, both got that refusal.
        """
        pending = dict(descriptions)
        deadline = self._clock() + _ASSET_WAIT_SECONDS
        while True:
            for arn in list(pending):
                try:
                    self._glue.update_asset(Identifier=arn, Description=pending[arn])
                    del pending[arn]
                except (self._glue.exceptions.EntityNotFoundException,
                        self._glue.exceptions.ConcurrentModificationException):
                    continue
            if not pending:
                return
            if self._clock() >= deadline:
                raise RuntimeError(
                    f"{len(pending)} asset(s) could not be described within "
                    f"{_ASSET_WAIT_SECONDS}s of their tables being written (not yet "
                    f"registered, or being changed concurrently): {sorted(pending)[:5]}"
                )
            self._sleep(_ASSET_RETRY_SECONDS)

    def publish_objects(self, *, data_source: str, engine: str,
                        objects: Iterable[SchemaObject]) -> dict[str, Any]:
        if engine not in _CLASSIFICATION_FROM_ENGINE:
            raise ValueError(
                f"Engine {engine!r} has no Glue classification. Known: "
                f"{sorted(_CLASSIFICATION_FROM_ENGINE)}"
            )
        if not data_source:
            raise ValueError("data_source is required.")

        wanted: dict[str, SchemaObject] = {}
        for t in objects:
            name = _catalog_name(engine, data_source, t.schema, t.name)
            if name in wanted:
                other = wanted[name]
                raise ValueError(
                    f"{other.schema}.{other.name} and {t.schema}.{t.name} would both "
                    f"be catalogued as {name}."
                )
            wanted[name] = t

        existing = self._published(data_source, engine)
        stats = {"created": 0, "updated": 0, "removed": 0, "kept": 0}

        for name, t in wanted.items():
            params = {
                "classification": _CLASSIFICATION_FROM_ENGINE[engine],
                "source_database": data_source,
                "source_engine": engine,
                "source_kind": t.kind,
            }
            table_input = {
                "Name": name,
                "TableType": "EXTERNAL_TABLE",
                "StorageDescriptor": {"Location": _location(data_source, t.schema, t.name)},
            }
            if name in existing:
                # UpdateTable replaces Parameters, so the existing ones (and whether it is ours) are kept.
                table_input["Parameters"] = {**existing[name], **params}
                self._glue.update_table(DatabaseName=self._index_database, TableInput=table_input)
                stats["updated"] += 1
            else:
                table_input["Parameters"] = {**params, OWNER_PARAMETER: self._owner}
                self._glue.create_table(DatabaseName=self._index_database, TableInput=table_input)
                stats["created"] += 1

        self._describe_assets({
            self._table_arn(name): t.definition
            for name, t in wanted.items()
        })

        for name in sorted(existing.keys() - wanted.keys()):
            if not self._ours(existing[name]):
                stats["kept"] += 1
                continue
            self._glue.delete_table(DatabaseName=self._index_database, Name=name)
            stats["removed"] += 1

        logger.info("Published %s (%s) into %s: %s", data_source, engine,
                    self._index_database, stats)
        return stats


def provider_from_env() -> GlueDataCatalogSchemaProvider:
    return GlueDataCatalogSchemaProvider(
        index_database=require_env("SCHEMA_INDEX_DATABASE"),
        region=require_env("AWS_REGION"),
        catalog_id=require_env("CATALOG_ID"),
        owner=require_env("STACK_NAME"),
    )


# ── Lambda handler (search) ─────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    operation = event.get("operation", "")
    args = event.get("args", {})

    # The registered databases' versions, for the connection registry: where each is, its
    # name and statement limits, never its scanner secret. With include_disabled, the disabled
    # versions too, by name and why each is refused, without where any of them is.
    if operation == "list_databases":
        from adam.schema_provider import scan_targets
        if args.get("include_disabled"):
            return {"response": {"active": scan_targets.databases(), "disabled": scan_targets.disabled_databases()}}
        return {"response": scan_targets.databases()}
    if operation == "get_database":
        from adam.schema_provider import scan_targets
        target = scan_targets.get(str(args.get("targetId") or ""))
        if target is None or not target.get("name") or target["disabled"]:
            return {"error": scan_targets.refusal(target)}
        return {"response": scan_targets.database(target)}

    if operation == "search_objects":
        if "visible" not in args:
            raise RuntimeError(
                "search_objects requires 'visible': the tables this caller may see. "
                "There is no unfiltered search."
            )
        visible = frozenset(VisibleObject.from_dict(v) for v in args["visible"])
        return {"response": provider_from_env().search_objects(
            args.get("query", ""), visible=visible, limit=args["limit"],
        )}

    return {"error": f"Unknown operation: {operation}"}
