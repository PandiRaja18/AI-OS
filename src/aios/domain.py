"""A domain pack: your data, your queries, your prompts - as configuration.

The engine is domain-neutral. What makes a run about supplier reviews rather
than audit is four things: which database it reads, which questions it may ask
of it, which documents it searches, and how the agents are told to behave.

All four live in a TOML file, so pointing this at real data is editing config,
not editing Python. The demo fixtures remain available and untouched; a run uses
a domain pack only when one is configured.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from aios.files import TABULAR

SELECT_ONLY = ("select", "with")


class NamedQuery(BaseModel):
    """One question the data agent is allowed to ask.

    The model chooses a query by *name* and supplies parameters; it never writes
    SQL. That is the difference between a tool and an open database connection.
    """

    description: str
    params: list[str] = Field(default_factory=list)
    sql: str

    @field_validator("sql")
    @classmethod
    def must_be_read_only(cls, value: str) -> str:
        head = value.strip().lstrip("(").split(None, 1)
        if not head or head[0].lower() not in SELECT_ONLY:
            raise ValueError(
                "a named query must start with SELECT or WITH; this platform "
                "never issues writes on your behalf"
            )
        forbidden = {"insert", "update", "delete", "drop", "alter", "truncate", "grant"}
        words = {word.strip("();,").lower() for word in value.split()}
        overlap = words & forbidden
        if overlap:
            raise ValueError(f"write keyword in a named query: {sorted(overlap)}")
        return value


class DataSource(BaseModel):
    """Where the data agent reads from.

    `files` points at a folder of spreadsheets, which are ingested into a
    throwaway database at run start. `sqlite` and `postgres` connect to a live
    system, always read-only.
    """

    kind: str = "sqlite"
    dsn: str = ""
    path: str = ""
    allow_adhoc_queries: bool = False

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value: str) -> str:
        if value not in ("sqlite", "postgres", "files"):
            raise ValueError(
                f"unsupported data source kind: {value} "
                "(expected sqlite, postgres or files)"
            )
        return value

    @model_validator(mode="after")
    def coherent(self) -> "DataSource":
        if self.kind == "files":
            if not self.path:
                raise ValueError("a files data source needs a path")
        elif not self.dsn:
            raise ValueError(f"a {self.kind} data source needs a dsn")

        if self.allow_adhoc_queries and self.kind != "files":
            raise ValueError(
                "ad-hoc SQL is only allowed against an ingested copy "
                "(kind = 'files'), never against a live database"
            )
        return self


class DocumentSource(BaseModel):
    """Where the research agent reads from.

    The default glob covers the document types that can be read: Markdown,
    plain text, PDF and Word.
    """

    path: str
    glob: str = "*"


class AgentPrompts(BaseModel):
    """What each agent is told it is for. Overrides the built-in audit prompts."""

    research: str | None = None
    data: str | None = None
    reporting: str | None = None


class Capabilities(BaseModel):
    """What the planner is told each agent can reach.

    Plans are only as good as this description. If it is stale, the planner
    writes tasks for tools that do not exist.
    """

    research: str = "policy documents and prior memos"
    data: str = "the warehouse, through named queries"
    reporting: str = "synthesis of the other agents' results"


class DomainPack(BaseModel):
    """Everything that makes a run about one domain rather than another."""

    name: str
    description: str = ""
    data_source: DataSource | None = None
    documents: DocumentSource | None = None
    queries: dict[str, NamedQuery] = Field(default_factory=dict)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    prompts: AgentPrompts = Field(default_factory=AgentPrompts)

    @classmethod
    def load(cls, path: str | Path) -> "DomainPack":
        """Read a domain pack from TOML."""
        source = Path(path)
        if not source.exists():
            raise FileNotFoundError(f"no domain pack at {source}")
        with source.open("rb") as handle:
            return cls.model_validate(tomllib.load(handle))

    def check(self) -> list[str]:
        """Problems that would make this pack useless at runtime."""
        problems: list[str] = []
        if self.data_source is None and self.documents is None:
            problems.append("the pack declares no data source and no documents")
        source = self.data_source
        if source is not None:
            if source.kind == "sqlite" and not Path(source.dsn).exists():
                problems.append(f"database not found: {source.dsn}")
            if source.kind == "files":
                folder = Path(source.path)
                if not folder.exists():
                    problems.append(f"data folder not found: {folder}")
                elif not any(
                    path.suffix.lower() in TABULAR for path in folder.rglob("*")
                ):
                    problems.append(
                        f"no spreadsheets (.csv/.tsv/.xlsx) under {folder}"
                    )
            if not self.queries and not source.allow_adhoc_queries:
                problems.append(
                    "a data source with no named queries and no ad-hoc access: "
                    "the data agent would have nothing it is allowed to ask"
                )
        if self.documents is not None:
            folder = Path(self.documents.path)
            if not folder.exists():
                problems.append(f"document folder not found: {folder}")
            elif not any(folder.glob(self.documents.glob)):
                problems.append(
                    f"no files matching {self.documents.glob} in {folder}"
                )
        return problems


TEMPLATE = '''# Domain pack for "{name}".
# Point this at your own data, then run:
#     aios domain check --file {filename}
#     python scripts/run_real.py "your goal" --domain {filename} --local

name = "{name}"
description = "What this domain is for, in one line."

# What the planner is told each agent can reach. Plans are only as good as this.
[capabilities]
research = "policy documents, prior review memos"
data = "the {name} warehouse, through the named queries below"
reporting = "synthesis of the other agents' results into a reviewable document"

# The database the data agent reads. Use a READ-ONLY credential.
[data_source]
kind = "sqlite"                      # sqlite | postgres
dsn = "C:/path/to/your.db"           # or postgresql://user:pass@host/db

# The corpus the research agent searches.
[documents]
path = "C:/path/to/your/documents"
glob = "*.md"

# One entry per question the data agent may ask. The model picks a query by
# name and supplies parameters - it never writes SQL.
[queries.example_exceptions]
description = "Records above a threshold in a period that were never approved"
params = ["period", "threshold"]
sql = """
SELECT id, counterparty, amount, booked_on
FROM your_table
WHERE period = :period
  AND approval_ref IS NULL
  AND amount >= :threshold
ORDER BY amount DESC
"""

# Optional: replace what each agent is told it is for.
# [prompts]
# research = "You are the Research agent for ..."
# data = "You are the Data agent for ..."
# reporting = "You are the Reporting agent for ..."
'''


def scaffold(name: str, path: Path) -> Path:
    """Write a starter domain pack."""
    path.write_text(
        TEMPLATE.format(name=name, filename=path.name), encoding="utf-8"
    )
    return path


def scaffold_from_folder(name: str, path: Path, folder: Path, workspace: Path) -> Path:
    """Write a pack that already knows the tables and columns in `folder`.

    Writing SQL for your own spreadsheets is the friction that stops people
    getting to a first run, so this inspects the files and fills in the real
    names, plus one starter query per table.
    """
    from aios.files import ingest_folder, inventory

    found = inventory(folder)
    ingested = ingest_folder(folder, workspace / "ingested" / f"{name}-scaffold.db")

    lines = [
        f"# Domain pack for \"{name}\", generated from {folder}.",
        "#",
        f"# Found {len(found['tabular'])} spreadsheet(s), "
        f"{len(found['documents'])} document(s), "
        f"{len(found['ignored'])} ignored file(s).",
        "# Edit the queries below to ask the questions your review actually needs.",
        "",
        f'name = "{name}"',
        'description = "What this domain is for, in one line."',
        "",
        "[capabilities]",
        'research = "the documents in this folder"',
        'data = "the spreadsheets in this folder, via describe_schema and named queries"',
        'reporting = "synthesis of the other agents\' results into a reviewable document"',
        "",
        "[data_source]",
        'kind = "files"',
        f'path = "{folder.as_posix()}"',
        "# Ad-hoc SELECT runs against an ingested copy, never your files.",
        "allow_adhoc_queries = true",
        "",
        "[documents]",
        f'path = "{folder.as_posix()}"',
        "",
    ]

    if not ingested.tables:
        lines += [
            "# No spreadsheets were found, so there are no queries to start from.",
            "# Add .csv or .xlsx files and re-run: aios domain init --from-folder",
        ]
    for table in ingested.tables:
        columns = ", ".join(table.columns)
        lines += [
            f"# {table.source}: {table.rows} row(s), columns: {columns}",
            f"[queries.all_{table.name}]",
            f'description = "Rows from {table.source}, most recent first"',
            'params = ["limit"]',
            'sql = """',
            f"SELECT {columns}",
            f"FROM {table.name}",
            "LIMIT :limit",
            '"""',
            "",
        ]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def load_pack(settings) -> DomainPack | None:
    """The domain pack this deployment is configured for, if any."""
    if settings.domain_file is None:
        return None
    return DomainPack.load(settings.domain_file)
