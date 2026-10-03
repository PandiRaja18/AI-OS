# Running on a self-hosted model, with real data

Everything in this platform depends on one thing: **a model call returns a valid
typed object, not prose.** A hosted frontier model gets that right nearly always.
A self-hosted model needs help, and this page is about giving it that help.

---

## 1. Which servers work

Any server with an OpenAI-compatible `/v1/chat/completions` endpoint, plus Ollama's
native API:

| Server | `--api-style` | Schema support |
|---|---|---|
| **vLLM** | `openai` | `response_format: json_schema` (guided decoding) — best option |
| **Ollama** | `ollama` | `format: <schema>` — easiest to start with |
| **llama.cpp** (`llama-server`) | `openai` | `json_schema` on recent builds |
| **TGI**, **LM Studio**, **LocalAI** | `openai` | varies — check the build |

## 2. Pick a model that can hold a schema

This is the decision that determines whether the whole thing works.

| Size | Realistic outcome |
|---|---|
| 7–8B | The agents mostly work. **The Planner does not.** Expect invalid DAGs and repair loops. |
| 14B | Workable with constrained decoding on. Plans are shallow. |
| **32B instruct** | The practical floor. Qwen2.5-32B-Instruct and similar work. |
| 70B+ | Comparable to a hosted model for this workload. |

Two hard requirements: an **instruction-tuned** model (base models cannot follow
the contract), and **constrained decoding turned on** at the server. Without
constrained decoding even a 70B will drift out of schema often enough to be
unusable.

The client sends a flattened schema (`$ref`/`$defs` inlined, every object closed
with `additionalProperties: false`) because several backends silently ignore
references and then generate whatever they like.

## 3. Configure it

```bash
export AIOS_LLM_PROVIDER=local
export AIOS_LLM_BASE_URL=http://localhost:11434    # or your vLLM host
export AIOS_LLM_MODEL=qwen2.5:32b-instruct
export AIOS_LLM_API_STYLE=ollama                   # or openai
export AIOS_LLM_MAX_REPAIRS=2
export AIOS_LLM_TIMEOUT_SECONDS=300                # local models are slower

# turn off the demo's deliberate sabotage
export AIOS_INJECTED_LEDGER_FAILURES=0
export AIOS_TOKEN_SECRET=$(openssl rand -hex 32)
```

A self-hosted model prices at **zero dollars** (`local:` models are free in the
budget meter — you pay in GPU time, not tokens). The **token ceilings still
apply**, and they are what stops a runaway run.

## 4. Point the tools at your data

This is the real work. [`mcp_gateway/tools.py`](../src/aios/mcp_gateway/tools.py):

- **`sql_query`** — keep the named-query pattern. The model picks a query *name*
  and passes parameters, so it cannot emit arbitrary SQL. Write one named query
  per question your domain asks. Use a **read-only** credential.
- **`doc_search`** — point at your corpus instead of `data/policies/`.
- **Delete `peer_benchmark` and `ledger_lookup`** — they exist only to fail.

Then rewrite the four prompts that carry the domain: the three agent role prompts
in [`agents/`](../src/aios/agents/), and the agent-capability list in
[`planner.py`](../src/aios/orchestration/planner.py) lines 29-30. The planner can
only produce good plans if that list truthfully describes what each agent reaches.

## 5. Preflight, then run

```bash
python scripts/run_real.py --check --local
```

It refuses to continue while any of these is true: injected failures are on, a
tool is forced offline, the document corpus is missing, or the model cannot
return valid JSON for a schema. It warns (but continues) if demo tools are still
registered or `sql_query` still points at the demo database.

```bash
python scripts/run_real.py "Prepare the Q3 supplier review" \
    --local --tenant acme --reviewer cfo
```

The trace streams as it runs. The run stops at the sign-off gate and prints the
draft; approve it in the console (`aios serve`) or re-run with `--approve`.

## 6. When it misbehaves

| Symptom | Cause | Fix |
|---|---|---|
| `could not produce valid Plan after 3 attempts` | Model too small for planning | Bigger model, or raise `AIOS_LLM_MAX_REPAIRS` |
| Many `repaired x1` lines in the trace | Constrained decoding is off | Enable guided decoding on the server |
| `guided decoding backend unavailable` | vLLM started without it | Restart with `--guided-decoding-backend outlines` |
| Plans have 1–2 tasks and miss the point | Role prompts still describe the audit demo | Rewrite the four prompts |
| Agents call tools that do not exist | Capability list in the planner prompt is stale | Update `planner.py` lines 29-30 |
| Every run takes 20+ minutes | Normal for a local 32B | Raise the timeout, or use a smaller model for the tool-plan step |

## 7. What this does not change

The model is the only thing you swapped. The scheduler, the retry and
degradation policy, conflict detection, the authorization gateway, the human gate
and the promotion policy are identical — which is the point of routing every
model call through one typed interface.

One consequence worth stating plainly: **the model sees whatever your tools
return.** With a self-hosted model that data never leaves your network, which is
usually the reason for self-hosting in the first place.
