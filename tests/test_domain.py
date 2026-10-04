"""Domain packs: pointing the engine at your own files instead of the demo."""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from aios.config import Settings
from aios.domain import (
    DataSource,
    DomainPack,
    InvalidPack,
    NamedQuery,
    scaffold,
    scaffold_from_folder,
)
from aios.files import ingest_folder, inventory, read_docx, read_document
from aios.mcp_gateway import McpGateway, Principal, ToolAccessDenied, build_run_tools
from aios.mcp_gateway.domain_tools import UnsafeQuery, assert_read_only
from aios.observability import TraceStore
from aios.orchestration.state import AgentType
from aios.platform.tenancy import grants_from_tools

DATA = Principal.for_agent(AgentType.DATA)
RESEARCH = Principal.for_agent(AgentType.RESEARCH)

DOCX_XML = (
    '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org'
    '/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Dual approval above '
    "25,000 USD.</w:t></w:r></w:p></w:body></w:document>"
)


@pytest.fixture
def user_folder(tmp_path: Path) -> Path:
    """A folder shaped like a real user's: messy headers, mixed file types."""
    folder = tmp_path / "userdata"
    folder.mkdir()
    with (folder / "Supplier Spend 2026.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["Txn ID", "Supplier Name", "Amount (USD)", "Approval Ref"])
        writer.writerow(["T-1", "Northwind", "28400", ""])
        writer.writerow(["T-2", "Northwind", "27300", ""])
        writer.writerow(["T-3", "Cobalt", "12500", "AP-77"])
    (folder / "policy.md").write_text(
        "# Policy\n\nDual approval above 25,000 USD.", encoding="utf-8"
    )
    with zipfile.ZipFile(folder / "memo.docx", "w") as archive:
        archive.writestr("word/document.xml", DOCX_XML)
    (folder / "logo.png").write_bytes(b"\x89PNG")
    return folder


@pytest.fixture
def pack(user_folder: Path, tmp_path: Path) -> DomainPack:
    path = tmp_path / "domain.toml"
    path.write_text(
        f'''
name = "supplier"
[data_source]
kind = "files"
path = "{user_folder.as_posix()}"
allow_adhoc_queries = true
[documents]
path = "{user_folder.as_posix()}"
[queries.unapproved_spend]
description = "Spend with no approval reference above a threshold"
params = ["threshold"]
sql = """
SELECT supplier_name, SUM(CAST(amount_usd AS REAL)) AS total
FROM supplier_spend_2026
WHERE approval_ref = '' AND CAST(amount_usd AS REAL) >= :threshold
GROUP BY supplier_name
"""
''',
        encoding="utf-8",
    )
    return DomainPack.load(path)


# --- reading a real folder ----------------------------------------------------


def test_a_folder_is_sorted_into_tables_documents_and_the_rest(user_folder: Path):
    found = inventory(user_folder)
    assert found["tabular"] == ["Supplier Spend 2026.csv"]
    assert sorted(found["documents"]) == ["memo.docx", "policy.md"]
    assert found["ignored"] == ["logo.png"]


def test_messy_spreadsheet_headers_become_usable_column_names(
    user_folder: Path, tmp_path: Path
):
    result = ingest_folder(user_folder, tmp_path / "ingested.db")
    table = result.tables[0]

    assert table.name == "supplier_spend_2026"
    assert table.columns == ["txn_id", "supplier_name", "amount_usd", "approval_ref"]
    assert table.rows == 3


def test_word_documents_read_without_an_extra_dependency(user_folder: Path):
    assert "Dual approval above 25,000 USD." in read_docx(user_folder / "memo.docx")
    assert read_document(user_folder / "policy.md").startswith("# Policy")


def test_an_unreadable_file_is_skipped_not_fatal(user_folder: Path, tmp_path: Path):
    (user_folder / "broken.csv").write_bytes(b"\x00\x01\x02")
    result = ingest_folder(user_folder, tmp_path / "ingested.db")
    assert [table.name for table in result.tables] == ["supplier_spend_2026"]


# --- the pack contract --------------------------------------------------------


def test_a_named_query_that_writes_is_refused_at_load():
    with pytest.raises(ValidationError, match="SELECT"):
        NamedQuery(description="bad", sql="DELETE FROM spend")
    with pytest.raises(ValidationError, match="write keyword"):
        NamedQuery(description="bad", sql="SELECT 1 FROM t; DROP TABLE t")


def test_a_files_source_needs_a_folder_and_a_database_needs_a_dsn():
    with pytest.raises(ValidationError, match="needs a path"):
        DataSource(kind="files")
    with pytest.raises(ValidationError, match="needs a dsn"):
        DataSource(kind="sqlite")


def test_adhoc_sql_is_only_offered_against_an_ingested_copy():
    with pytest.raises(ValidationError, match="ingested copy"):
        DataSource(kind="sqlite", dsn="live.db", allow_adhoc_queries=True)
    assert DataSource(kind="files", path="x", allow_adhoc_queries=True).kind == "files"


def test_check_reports_what_would_break_at_runtime(tmp_path: Path):
    missing = DomainPack(
        name="x", data_source=DataSource(kind="files", path=str(tmp_path / "nope"))
    )
    assert any("not found" in problem for problem in missing.check())

    no_questions = DomainPack(
        name="x", data_source=DataSource(kind="sqlite", dsn=str(tmp_path))
    )
    assert any("nothing it is allowed to ask" in p for p in no_questions.check())


def test_a_scaffolded_pack_parses(tmp_path: Path):
    path = scaffold("mydomain", tmp_path / "domain.toml")
    assert "mydomain" in path.read_text(encoding="utf-8")


def test_a_windows_path_in_double_quotes_says_what_to_change(tmp_path: Path):
    """TOML reads a backslash as an escape, and the raw error is unactionable."""
    path = tmp_path / "windows.toml"
    path.write_text(
        'name = "x"\n\n[documents]\npath = "C:\\work\\pdfs"\n', encoding="utf-8"
    )
    with pytest.raises(InvalidPack) as raised:
        DomainPack.load(path)

    message = str(raised.value)
    assert "Unescaped" in message
    assert 'path = "C:/work/data"' in message, "it must show the forward-slash form"
    assert "single quotes" in message


def test_a_windows_path_in_single_quotes_is_accepted(tmp_path: Path):
    path = tmp_path / "literal.toml"
    path.write_text(
        "name = \"x\"\n\n[documents]\npath = 'C:\\work\\pdfs'\n", encoding="utf-8"
    )
    assert DomainPack.load(path).documents.path == "C:\\work\\pdfs"


def test_a_documents_only_folder_generates_a_usable_pack(tmp_path: Path):
    """A folder of PDFs has nothing to query, so the pack declares no data."""
    folder = tmp_path / "pdfs"
    folder.mkdir()
    (folder / "policy.md").write_text("Dual approval above 25,000.", encoding="utf-8")

    path = scaffold_from_folder("docs", tmp_path / "d.toml", folder, tmp_path / "ws")
    generated = DomainPack.load(path)

    assert generated.check() == []
    assert generated.data_source is None, "no spreadsheets means no data source"
    assert generated.documents is not None
    assert "Never assign a task to the data agent" in generated.capabilities.data


def test_a_folder_with_spreadsheets_still_gets_a_data_source(
    user_folder: Path, tmp_path: Path
):
    path = scaffold_from_folder("mixed", tmp_path / "m.toml", user_folder, tmp_path / "ws")
    generated = DomainPack.load(path)

    assert generated.check() == []
    assert generated.data_source is not None
    assert generated.queries, "a starter query per table"


def test_a_usable_pack_reports_no_problems(pack: DomainPack):
    assert pack.check() == []


# --- the tools a pack produces ------------------------------------------------


def tools_for(pack: DomainPack, settings: Settings):
    return build_run_tools(settings, "run-test", pack=pack)


def test_a_pack_publishes_discovery_query_and_search_tools(
    pack: DomainPack, settings: Settings
):
    names = {tool.name for tool in tools_for(pack, settings)}
    assert names == {
        "describe_schema",
        "adhoc_query",
        "sql_query",
        "doc_search",
        "render_report",
        "record_signoff",
    }
    assert "ledger_lookup" not in names, "demo tools must not leak into a domain"


def test_the_data_agent_can_discover_and_then_query_your_files(
    pack: DomainPack, settings: Settings, trace: TraceStore
):
    gateway = McpGateway(tools_for(pack, settings), trace)

    schema = gateway.call(DATA, "describe_schema")
    assert schema[0]["columns"] == [
        "txn_id",
        "supplier_name",
        "amount_usd",
        "approval_ref",
    ]

    rows = gateway.call(DATA, "sql_query", query="unapproved_spend", threshold=25000)
    assert rows == [{"supplier_name": "Northwind", "total": 55700.0}]


def test_document_search_covers_word_as_well_as_markdown(
    pack: DomainPack, settings: Settings, trace: TraceStore
):
    gateway = McpGateway(tools_for(pack, settings), trace)
    hits = gateway.call(RESEARCH, "doc_search", query="dual approval threshold")
    assert {hit["document"] for hit in hits} == {"policy.md", "memo.docx"}


def test_a_missing_query_parameter_is_an_error_not_a_wrong_answer(
    pack: DomainPack, settings: Settings, trace: TraceStore
):
    gateway = McpGateway(tools_for(pack, settings), trace)
    with pytest.raises(Exception, match="needs parameters"):
        gateway.call(DATA, "sql_query", query="unapproved_spend")


def test_the_research_agent_cannot_reach_the_database(
    pack: DomainPack, settings: Settings, trace: TraceStore
):
    gateway = McpGateway(tools_for(pack, settings), trace)
    with pytest.raises(ToolAccessDenied):
        gateway.call(RESEARCH, "adhoc_query", sql="SELECT 1")


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE spend",
        "UPDATE spend SET amount_usd = 0",
        "SELECT 1; DELETE FROM spend",
        "INSERT INTO spend VALUES (1)",
        "PRAGMA table_info(spend)",
    ],
)
def test_adhoc_sql_refuses_anything_that_is_not_a_read(statement: str):
    with pytest.raises(UnsafeQuery):
        assert_read_only(statement)


def test_adhoc_sql_allows_a_plain_select(pack: DomainPack, settings: Settings, trace):
    gateway = McpGateway(tools_for(pack, settings), trace)
    rows = gateway.call(
        DATA, "adhoc_query", sql="SELECT COUNT(*) AS n FROM supplier_spend_2026"
    )
    assert rows == [{"n": 3}]


def test_grants_are_derived_from_the_tools_a_domain_publishes(
    pack: DomainPack, settings: Settings
):
    """The bug this guards: a pack's own tools were dropped by apply_grants."""
    tools = tools_for(pack, settings)
    grants = {grant.tool: grant for grant in grants_from_tools("acme", tools)}

    assert "describe_schema" in grants
    assert grants["describe_schema"].allowed_agents == ("data",)
    assert grants["record_signoff"].allowed_kinds == ("human",)


def test_without_a_pack_the_demo_registry_is_used(settings: Settings):
    names = {tool.name for tool in build_run_tools(settings, "run-test")}
    assert "ledger_lookup" in names and "describe_schema" not in names
