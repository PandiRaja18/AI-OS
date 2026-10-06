# Running on your own data, with a local model

Two commands. No configuration files to write, no environment variables.

```bash
aios use C:/work/my-files      # point at a folder
aios serve                     # http://127.0.0.1:8000
```

`aios use` inspects the folder, writes a domain pack for it, records it as the
active domain, and grants your tenant exactly the tools that domain publishes.
The console header then shows **`domain: my-files`** instead of **`DEMO DATA`**,
so you can always tell which data is live.

`aios use --demo` switches back. `aios use` with no argument says which is active.

---

## 1. Install a model

```bash
winget install Ollama.Ollama       # or: brew install ollama
ollama pull qwen2.5:32b-instruct
ollama serve
```

**Model size decides whether this works at all.** Every call must return a valid
typed object:

| Size | What happens |
|---|---|
| 7-8B | Agents mostly work, **the planner does not** |
| 14B | Workable, plans are shallow |
| **32B instruct** | The practical floor |
| 70B+ | Comparable to a hosted model here |

vLLM, llama.cpp, TGI and LM Studio work too - set `AIOS_LLM_API_STYLE=openai`.

```bash
set AIOS_LLM_PROVIDER=local
set AIOS_LLM_MODEL=qwen2.5:32b-instruct
```

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

## 3. Tune it, only if you want to

`aios use` already wrote a pack at `.aios/domains/<name>.toml` with your real
tables and columns. You never have to open it. Two reasons you might:

**Better questions.** The generated queries just list rows. Replace them with
the questions your review actually asks.

**The agents still think they are auditors.** The built-in role prompts are
written for audit, so a CV gets reviewed like a compliance file. Add a
`[prompts]` block:

```toml
[prompts]
research = "You extract claims from a candidate's CV: roles, dates, scope."
reporting = "You write a hiring assessment: evidence, gaps, what to probe."
```

Re-check after editing, and note that `aios use` keeps your edits unless you
pass `--force`:

```bash
aios domain check
```

## 4. Run it

In the browser:

```bash
aios serve                 # http://127.0.0.1:8000
```

Sign in as the tenant `aios use` set up (`me` by default), check the header says
**`domain: <your folder>`** and not **`DEMO DATA`**, then submit a goal. The
trace streams as it runs, and the review inbox holds it for your decision.

Or from the terminal, which also runs a preflight first:

```bash
python scripts/run_real.py "Review this data and report what needs attention" --local
```

Preflight **refuses to continue** if the demo's sabotage switches are on, the
pack is unusable, or the model cannot return valid JSON for a schema. No
`--domain` needed: it uses whatever `aios use` selected.

## 5. What it looks like when it works

```
 4  plan           planner     3 tasks: Establish the rule, scan the spend, then report.
 8  tool_call      research    doc_search(query='dual approval threshold', limit=3)
10  agent_result   research    Policy requires dual approval at or above 25,000 USD.
14  tool_call      data        describe_schema()
15  tool_call      data        sql_query(query='unapproved_spend', threshold=25000)
17  agent_result   data        Northwind has 55,700 USD of unapproved spend.
    status: awaiting_signoff
```

## 6. Limits worth knowing before you rely on it

- **A folder is a snapshot, not a feed.** It is re-ingested when a file changes,
  not continuously. For live data use `kind = "sqlite"` or `"postgres"`.
- **Scanned PDFs need OCR**, which this does not do. Text-layer PDFs are fine.
- **Everything ingests as text.** Cast in your SQL: `CAST(amount_usd AS REAL)`.
- **Conflict detection compares exact strings.** `55,700` and `55700.00` will not
  match, so two agents reporting the same figure differently will not be caught.
- **Confidence is self-reported** by the model and is not calibrated. Keep the
  human gate on.
