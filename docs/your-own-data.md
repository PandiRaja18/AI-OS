# Running on your own data, with a local model

Nothing here requires editing Python. You put files in a folder, describe them
in one TOML file, and run. The model runs on your machine, so your data does not
leave the network.

---

## 1. Install a model

```bash
# Ollama is the easiest start
winget install Ollama.Ollama       # or: brew install ollama
ollama pull qwen2.5:32b-instruct
ollama serve
```

**Model size is the decision that determines whether this works.** The platform
requires every model call to return a valid typed object:

| Size | What happens |
|---|---|
| 7–8B | Agents mostly work, **the planner does not**. Expect invalid plans. |
| 14B | Workable. Plans are shallow. |
| **32B instruct** | The practical floor. |
| 70B+ | Comparable to a hosted model for this workload. |

vLLM, llama.cpp, TGI and LM Studio also work — use `--api-style openai`.

## 2. Put your files in a folder

```
C:/work/supplier-review/
├── spend-q3.csv          → becomes a queryable table
├── vendors.xlsx          → one table per sheet
├── policy.docx           → searchable text
├── prior-review.pdf      → searchable text (text layer only)
└── notes.md
```

| Extension | Used as |
|---|---|
| `.csv` `.tsv` `.xlsx` `.xlsm` | **Data** — ingested into a throwaway SQLite copy, one table per file or sheet |
| `.md` `.txt` `.pdf` `.docx` `.rst` | **Documents** — searched as text |
| anything else | Ignored, and listed so you know it was |

Messy headers are handled: `Amount (USD)` becomes `amount_usd`. A binary file
with a `.csv` name is skipped with a reason rather than becoming a junk table.

You can also copy files in from elsewhere:

```bash
aios domain add "C:/Downloads/extract.xlsx" "C:/Reports/*.pdf"
```

## 3. Describe it

```bash
aios domain init supplier          # writes domain.toml
```

Edit the four things that matter:

```toml
name = "supplier"

[capabilities]                      # what the planner is told each agent reaches
research = "supplier policy documents and prior reviews"
data = "supplier spend extracts, via describe_schema and named queries"
reporting = "synthesis into a reviewable document"

[data_source]
kind = "files"
path = "C:/work/supplier-review"
allow_adhoc_queries = true          # safe here: it queries the ingested copy

[documents]
path = "C:/work/supplier-review"

[queries.unapproved_spend]
description = "Supplier spend with no approval reference above a threshold"
params = ["threshold"]
sql = """
SELECT supplier, SUM(CAST(amount_usd AS REAL)) AS total
FROM spend WHERE approval_ref = '' AND CAST(amount_usd AS REAL) >= :threshold
GROUP BY supplier ORDER BY total DESC
"""
```

**Windows paths need care.** TOML treats `\` as an escape character, so
`path = "C:\work\data"` fails with `Unescaped '\' in a string`. Write it either
way below — forward slashes work fine on Windows:

```toml
path = "C:/work/data"      # forward slashes
path = 'C:\work\data'      # single quotes: a TOML literal string
```

**`capabilities` is the one people get wrong.** The planner writes tasks against
that description. If it claims an agent can reach something it cannot, the
planner produces tasks that are guaranteed to fail.

**Documents only, no spreadsheets?** Omit the `[data_source]` block entirely and
tell the planner there is no database, or it will write tasks for a data agent
that has no tools:

```toml
[capabilities]
research = "the PDF documents in this folder, via doc_search"
data = "NOTHING. No database is configured. Never assign a task to the data agent."
reporting = "synthesis of the research agent's findings"

[documents]
path = "C:/work/review/pdfs"
```

`aios domain init --from-folder` writes exactly this when it finds no
spreadsheets.

**Named queries versus ad-hoc.** A named query is a question you have decided
the data agent may ask; the model supplies parameters and never writes SQL.
Ad-hoc SQL is offered *only* for `kind = "files"`, because it runs against a
disposable copy — it is refused outright against a live database. Either way
only `SELECT` is permitted, one statement, capped at 500 rows.

```bash
aios domain check                   # validates and shows what the agents will see
```

## 4. Run it

```bash
export AIOS_INJECTED_LEDGER_FAILURES=0     # off the demo's sabotage switches

python scripts/run_real.py "Review supplier spend for unapproved transactions" \
    --domain domain.toml --local \
    --base-url http://localhost:11434 --model qwen2.5:32b-instruct \
    --api-style ollama --tenant acme --reviewer cfo
```

Preflight runs first and **refuses to continue** if the demo switches are on,
the pack is unusable, or the model cannot return valid JSON for a schema. Then
the trace streams, the run stops at the sign-off gate, and the draft prints.

## 5. Or use the console

```bash
export AIOS_DOMAIN_FILE=C:/work/supplier-review/domain.toml
export AIOS_LLM_PROVIDER=local
export AIOS_LLM_MODEL=qwen2.5:32b-instruct

aios provision acme --reviewer cfo    # grants the tools your domain publishes
aios serve                            # http://127.0.0.1:8000
```

Submit from the browser, watch the trace stream, approve in the review inbox.

## 6. What it looks like when it works

```
 4  plan           planner     3 tasks: Establish the rule, scan the spend, then report.
 8  tool_call      research    doc_search(query='dual approval threshold', limit=3)
10  agent_result   research    Policy requires dual approval at or above 25,000 USD.
14  tool_call      data        describe_schema()
15  tool_call      data        sql_query(query='unapproved_spend', threshold=25000)
17  agent_result   data        Northwind has 55,700 USD of unapproved spend.
    status: awaiting_signoff
```

## 7. Limits worth knowing before you rely on it

- **A folder is a snapshot, not a feed.** It is re-ingested when a file changes,
  not continuously. For live data use `kind = "sqlite"` or `"postgres"`.
- **Scanned PDFs need OCR**, which this does not do. Text-layer PDFs are fine.
- **Everything ingests as text.** Cast in your SQL: `CAST(amount_usd AS REAL)`.
- **Conflict detection compares exact strings.** `55,700` and `55700.00` will not
  match, so two agents reporting the same figure differently will not be caught.
- **Confidence is self-reported** by the model and is not calibrated. Keep the
  human gate on.
