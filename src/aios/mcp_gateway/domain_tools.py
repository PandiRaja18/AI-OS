"""Tools built from a domain pack, so real data needs config and not code.

Three differences from the demo registry. The database is opened **read-only at
the driver** - SQLite with `mode=ro`, Postgres in a read-only transaction - so a
mistake in a query cannot write to a production system. A folder of
spreadsheets is ingested into a throwaway copy, which is the only place ad-hoc
SQL is ever offered, because nothing the model does there can reach the
original. And there are no deliberately-broken tools; those exist only to make
the demo's failure paths visible.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from aios.config import Settings
from aios.domain import DomainPack
from aios.files import DOCUMENTS, Ingestion, ingest_folder, read_document
from aios.mcp_gateway.registry import PrincipalKind, ToolSpec
from aios.mcp_gateway.tools import record_signoff, render_report
from aios.orchestration.state import AgentType

_RESEARCH = AgentType.RESEARCH.value
_DATA = AgentType.DATA.value
_REPORTING = AgentType.REPORTING.value

_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "for", "and", "or", "to", "in", "is", "what", "which"}
)
MAX_ROWS = 500
WRITE_KEYWORDS = frozenset(
    {"insert", "update", "delete", "drop", "alter", "truncate", "create",
     "replace", "attach", "pragma", "grant", "vacuum"}
)


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in _STOPWORDS and len(token) > 2
    ]


class UnsafeQuery(ValueError):
    """A statement that is not a plain read."""


def assert_read_only(sql: str) -> str:
    """Reject anything that is not a single SELECT."""
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise UnsafeQuery("one statement at a time")
    head = stripped.lstrip("(").split(None, 1)
    if not head or head[0].lower() not in ("select", "with"):
        raise UnsafeQuery("only SELECT (or WITH ... SELECT) is allowed")
    words = {word.strip("();,").lower() for word in stripped.split()}
    forbidden = words & WRITE_KEYWORDS
    if forbidden:
        raise UnsafeQuery(f"write keyword present: {sorted(forbidden)}")
    return stripped


def ingested_database(pack: DomainPack, settings: Settings) -> Ingestion:
    """Ingest the pack's folder, reusing the copy while no source file is newer."""
    folder = Path(pack.data_source.path)
    target = settings.workspace / "ingested" / f"{pack.name}.db"
    if target.exists():
        newest = max(
            (path.stat().st_mtime for path in folder.rglob("*") if path.is_file()),
            default=0.0,
        )
        if target.stat().st_mtime >= newest:
            return _describe_existing(target)
    return ingest_folder(folder, target)


def _describe_existing(database: Path) -> Ingestion:
    """Rebuild the inventory of an already-ingested copy."""
    from aios.files import Table

    result = Ingestion(database=database)
    with sqlite3.connect(database) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        for name in names:
            columns = [
                row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')
            ]
            count = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            result.tables.append(
                Table(name=name, source="(cached)", columns=columns, rows=count)
            )
    return result


class ReadOnlyQueries:
    """Runs the pack's named queries, and nothing else."""

    def __init__(self, kind: str, dsn: str, queries: dict) -> None:
        self._kind = kind
        self._dsn = dsn
        self._queries = queries

    @property
    def names(self) -> list[str]:
        return sorted(self._queries)

    def describe(self) -> str:
        return "; ".join(
            f"{name} (params: {', '.join(query.params) or 'none'}) - {query.description}"
            for name, query in sorted(self._queries.items())
        )

    def __call__(self, query: str, **params: Any) -> list[dict[str, Any]]:
        named = self._queries.get(query)
        if named is None:
            raise KeyError(f"unknown query {query!r}; available: {self.names}")
        missing = [name for name in named.params if name not in params]
        if missing:
            raise KeyError(f"{query} needs parameters {missing}")
        supplied = {key: value for key, value in params.items() if key in named.params}
        return self.run(named.sql, supplied)

    def run(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self._kind == "postgres":
            return self._postgres(sql, params)
        return self._sqlite(sql, params)

    def _sqlite(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        uri = f"file:{Path(self._dsn).as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, params).fetchmany(MAX_ROWS)
        return [dict(row) for row in rows]

    def _postgres(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover - driver is optional
            raise RuntimeError(
                "postgres data source needs the psycopg driver: pip install psycopg"
            ) from error
        statement = re.sub(r":(\w+)", r"%(\1)s", sql)
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute(statement, params)
                return cursor.fetchmany(MAX_ROWS)


def search_documents(
    folder: Path, pattern: str, query: str, limit: int = 3
) -> list[dict[str, Any]]:
    """Lexical paragraph search across Markdown, text, PDF and Word files."""
    terms = set(_tokenize(query))
    hits: list[tuple[int, dict[str, Any]]] = []
    for path in sorted(folder.rglob(pattern)):
        if not path.is_file() or path.suffix.lower() not in DOCUMENTS:
            continue
        try:
            text = read_document(path)
        except Exception:
            continue
        for paragraph in (part.strip() for part in text.split("\n\n")):
            if not paragraph:
                continue
            score = sum(1 for token in _tokenize(paragraph) if token in terms)
            if score:
                hits.append((score, {"document": path.name, "excerpt": paragraph[:1200]}))
    hits.sort(key=lambda hit: hit[0], reverse=True)
    return [hit[1] for hit in hits[:limit]]


def build_domain_tools(
    pack: DomainPack,
    settings: Settings,
    run_id: str,
    report_writer: Callable[[str, Sequence[dict[str, str]]], str] | None = None,
) -> list[ToolSpec]:
    """Assemble the tool registry for one run of one domain."""
    tools: list[ToolSpec] = []
    source = pack.data_source

    if source is not None:
        if source.kind == "files":
            ingestion = ingested_database(pack, settings)
            queries = ReadOnlyQueries("sqlite", str(ingestion.database), pack.queries)
            schema = ingestion.describe()
            tools.append(
                ToolSpec(
                    name="describe_schema",
                    description=(
                        "List the tables and columns available to query. Call this "
                        "first when you do not know the shape of the data. "
                        "No arguments."
                    ),
                    handler=lambda: schema,
                    allowed_agents=frozenset({_DATA}),
                )
            )
            if source.allow_adhoc_queries:
                tools.append(
                    ToolSpec(
                        name="adhoc_query",
                        description=(
                            "Run a read-only SELECT against the ingested copy of "
                            "the data. Arguments: sql. Use describe_schema first. "
                            "Only SELECT is permitted and results are capped at "
                            f"{MAX_ROWS} rows."
                        ),
                        handler=lambda sql: queries.run(assert_read_only(sql), {}),
                        allowed_agents=frozenset({_DATA}),
                    )
                )
        else:
            queries = ReadOnlyQueries(source.kind, source.dsn, pack.queries)

        if pack.queries:
            tools.append(
                ToolSpec(
                    name="sql_query",
                    description=(
                        "Run a named, parameterized read-only query. Arguments: "
                        f"query (one of {queries.names}), plus that query's "
                        f"parameters. Queries: {queries.describe()}"
                    ),
                    handler=lambda query, **params: queries(query, **params),
                    allowed_agents=frozenset({_DATA}),
                )
            )

    if pack.documents is not None:
        folder = Path(pack.documents.path)
        pattern = pack.documents.glob
        tools.append(
            ToolSpec(
                name="doc_search",
                description=(
                    "Search the document corpus for relevant paragraphs. "
                    "Reads Markdown, text, PDF and Word files. "
                    "Arguments: query, limit."
                ),
                handler=lambda query, limit=3: search_documents(
                    folder, pattern, query, limit
                ),
                allowed_agents=frozenset({_RESEARCH, _REPORTING}),
            )
        )

    render = report_writer or (
        lambda title, sections: render_report(
            settings.report_dir, run_id, title, sections
        )
    )
    tools.append(
        ToolSpec(
            name="render_report",
            description=(
                "Render the final report. Arguments: title, sections "
                "(list of {heading, body})."
            ),
            handler=lambda title, sections: render(title, sections),
            allowed_agents=frozenset({_REPORTING}),
        )
    )
    tools.append(
        ToolSpec(
            name="record_signoff",
            description="Record a reviewer decision. Human principals only.",
            handler=lambda reviewer, approved, note=None: record_signoff(
                settings.workspace, run_id, reviewer, approved, note
            ),
            allowed_agents=frozenset(),
            allowed_kinds=frozenset({PrincipalKind.HUMAN}),
        )
    )
    return tools
