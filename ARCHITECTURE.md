# Architecture

## What this is

A LangGraph-based multi-agent orchestration platform. It takes a fuzzy,
high-level goal, autonomously decomposes it into a coordinated set of tasks,
runs them on specialised agents, and produces a verifiable, human-reviewable
output.

Four things it set out to demonstrate, and the file where each one lives:

| Goal | Where it lives | What it does |
|---|---|---|
| **Planning under ambiguity** — goal to task graph | `orchestration/planner.py` | Decomposes the goal into a DAG, rejects it if it has cycles, unknown dependencies or unknown agents, and replans when reality breaks it |
| **Coordination with real failure handling**, not just the happy path | `orchestration/coordinator.py` | Retry with backoff, degrade a non-critical task, replan around a blocked one; and when two agents disagree, re-query the less confident one |
| **A memory layer with an actual write policy**, not a generic vector store | `memory/semantic.py` | Promotion needs a human approval *and* a confidence bar; a contradicted fact is superseded, never overwritten |
| **Observability — every decision traceable** | `observability/` | Every plan, dispatch, tool call, retry, conflict and promotion is a traced event, queryable after the fact |

If a change does not serve one of those four, it does not belong in the engine.

## The engine, and everything else

Being honest about proportion, because the repository does not look like its
idea at a glance:

| Part | Lines | Share | What it is |
|---|---:|---:|---|
| **Engine** | 2,717 | **28%** | The idea. Planner, coordinator, graph, agents, A2A, memory, trace. |
| Hosting platform | 3,381 | 34% | Queue, leases, workers, tenancy, budgets, review inbox, API, console. Needed only for many users. |
| CLI and scripts | 1,539 | 16% | Entry points. |
| Own-data plumbing | 1,145 | 12% | Reading CSV, Excel, PDF and Word; domain packs. |
| Demo fixtures | 767 | 8% | Two synthetic domains. |
| Local model | 326 | 3% | Self-hosted inference. |

**The engine has not changed since the first commit.** `coordinator.py`,
`state.py`, `a2a.py` and `semantic.py` were last touched by the initial commit;
`agents/base.py` by the original upload. Three subsequent rounds of platform,
local-model and own-data work wrapped the engine without reaching inside it.
That separation is the point, and `git log` on those files is the evidence.

## How a run works

```
goal
 │
 ├─ Planner asks the model for a task DAG, and rejects it if it has cycles,
 │  unknown dependencies, unknown agents, or no terminal reporting task
 │
 ├─ Coordinator dispatches every task whose dependencies have resolved —
 │  in parallel, via LangGraph Send, one branch per task
 │
 ├─ Each agent: the model picks tool calls from the tools that agent is
 │  granted → the gateway authorizes and runs them → the model turns the
 │  results into {claims, confidence, evidence}, never prose
 │
 ├─ Coordinator compares claims across agents on (subject, metric).
 │  A contradiction re-queries the least confident claimant with the
 │  contradiction as context; if it survives, it escalates to the human
 │
 ├─ A failed tool is retried with backoff, then the task degrades (if it is
 │  not critical) or blocks and triggers a replan (if it is)
 │
 ├─ The graph interrupts at the sign-off gate and checkpoints. A paused run
 │  holds no worker and can be resumed in another process, another day
 │
 └─ On approval, claims that clear the confidence bar become durable facts,
    with provenance, and feed the next run's plan
```

## Where the pieces live

```
src/aios/
├── orchestration/        THE ENGINE
│   ├── state.py          typed run state: tasks, claims, conflicts
│   ├── planner.py        decomposition, DAG validation, replanning
│   ├── coordinator.py    scheduling policy — pure, no I/O, no framework
│   └── graph.py          LangGraph wiring: fan-out, routing, checkpoints
├── agents/               the two-phase agent contract
├── a2a.py                the envelope agents talk through
├── memory/               episodic (checkpoints) and durable (facts)
├── observability/        the trace every decision lands in
├── mcp_gateway/          tool authorization, timeouts, tracing
│
├── domain.py, files.py   your data: domain packs, CSV/PDF/XLSX/DOCX
├── llm.py, llm_local.py  model clients: hosted, self-hosted, replay
└── platform/             OPTIONAL: queue, workers, tenancy, API, console
```

`coordinator.py` is the file to read first. It imports no LangGraph and does no
I/O: it takes run state and returns a decision. That is why the scheduling rules
are unit-testable on their own, and why the graph file stays thin.

## Two demos

**`support`** — the headline. P1 tickets must close in seven days. The live
export says eight are late; last Monday's status report says four. The platform
notices, re-queries the weaker source, finds the report is a stale snapshot, and
escalates Payments. No domain knowledge required to follow it.

**`audit`** — more machinery, more background needed. Adds a flaky tool that
recovers on retry, an unreachable tool that degrades, and a variant where a dead
tool forces a replan.

## What it is not

Not a chatbot. Not a RAG pipeline. Not a workflow engine - you do not draw the
DAG, the planner derives it per goal and revises it when reality breaks it.

The v1 scope deliberately excluded three things: generality across arbitrary
domains, production-grade auth and multi-tenancy, and five shallow agents
instead of three deep ones. The first two were built later anyway, which is
why the repository is larger than the idea. They live in `domain.py` and
`platform/`, and neither changed the engine.

## Known weaknesses

- **Conflict identity is literal.** Claims match on an exact `(subject, metric)`
  string pair and exact values, so `55,700` and `55700.00` do not match. This is
  the weakest load-bearing part of the design.
- **Confidence is self-reported** by the model and uncalibrated, yet re-query
  targeting, escalation and promotion all key off it. That is why the human gate
  is mandatory.
- **Durable recall is lexical** plus recency and confidence. Fine at hundreds of
  facts, poor at tens of thousands. `FactStore.recall` is the seam.
- **Three grant fields are stored but not enforced**: `rate_limit_per_minute`,
  `secret_ref`, `max_wall_clock_seconds`.
