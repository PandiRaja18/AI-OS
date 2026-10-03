> Original high-level design, kept as written. `README.md` records what v1
> actually implements and where it deliberately stops short of this.

# Enterprise AI OS

A multi-agent operating layer for enterprises. Users hand it a high-level, ambiguous goal in natural language — *"Prepare quarterly audit review," "Investigate why churn spiked in APAC," "Onboard this new vendor and check compliance"* — and the system plans, coordinates, and executes across a fleet of specialized agents, producing a reviewable, evidence-backed result.

Think of it less as a chatbot and more as a kernel: the Planner is the scheduler, the Coordinator is the process manager, agents are processes, MCP is the syscall interface to the outside world, and the Memory Layer is persistent + working storage.

---

## Table of contents

1. [Why this exists](#why-this-exists)
2. [Architecture overview](#architecture-overview)
3. [Core components](#core-components)
4. [Agent catalogue](#agent-catalogue)
5. [Memory layer](#memory-layer)
6. [Protocols: A2A and MCP](#protocols-a2a-and-mcp)
7. [State management](#state-management)
8. [Failure handling](#failure-handling)
9. [Observability](#observability)
10. [Security model](#security-model)
11. [Tech stack](#tech-stack)
12. [Example run, end to end](#example-run-end-to-end)
13. [Repository structure](#repository-structure)
14. [Roadmap](#roadmap)

---

## Why this exists

Enterprise work is mostly: take a vague ask, figure out what it actually requires, pull data and context from a dozen systems, reconcile contradictions, produce something a human can sign off on. That loop — decompose, delegate, reconcile, synthesize — is a systems problem, not a prompting problem. This project treats it as one: a real scheduler, a real message protocol between workers, a real memory hierarchy, and real failure handling, all demonstrated on a non-trivial domain (audit/compliance-style workflows) rather than a toy demo.

---

## Architecture overview

```
┌───────────────────────────────────────────────────────────────────────┐
│                              User Interface                           │
│         goal input · task-graph view · live run trace · review/signoff│
└───────────────────────────────────┬───────────────────────────────────┘
                                    │
┌───────────────────────────────────▼───────────────────────────────────┐
│                          Orchestration Layer                          │
│  ┌────────────┐    ┌──────────────────┐     ┌───────────────────────┐ │
│  │  Planner   │──▶│  Task Graph /     │──▶ │     Coordinator       │ │
│  │  Agent     │    │  Run State       │     │  dispatch · retry ·   │ │
│  │ (decompose,│    │  (LangGraph nodes│     │  merge · conflict-    │ │
│  │  replan)   │    │  + checkpoints)  │     │  resolution · escalate│ │
│  └────────────┘    └──────────────────┘     └───────────┬───────────┘ │
└─────────────────────────────────────────────────────────┼─────────────┘
                                                          │  A2A envelopes
        ┌────────────┬────────────┬────────────┬──────────┐
        ▼            ▼            ▼            ▼          ▼  
 ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐
 │ Research  │ │   Data    │ │  Coding   │ │ Reporting │ │  (future  │
 │  Agent    │ │  Agent    │ │  Agent    │ │  Agent    │ │  agents)  │
 └─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └───────────┘
       │             │             │             │
       └─────────────┴──────┬──────┴─────────────┘
                            ▼
                  ┌────────────────────────┐
                  │      MCP Gateway       │
                  │  DBs · APIs · files ·  │
                  │  code sandbox · web    │
                  └───────────┬────────────┘
                              ▼
                  ┌────────────────────────┐
                  │      Memory Layer      │
                  │  episodic (per-run) +  │
                  │  semantic (durable)    │
                  └────────────────────────┘
```

Agents never call each other directly — every inter-agent interaction is a Coordinator-mediated A2A message. This keeps a single choke point for tracing, retries, and conflict resolution instead of an N×N mesh of ad-hoc agent calls.

---

## Core components

### Planner Agent
Takes the raw goal plus relevant durable-memory context and produces a **task DAG**: nodes with `task_id`, `description`, `assigned_agent_type`, `dependencies`, `success_criteria`, `priority`. The Planner is re-invoked (not just retried) whenever a downstream failure or contradiction invalidates one of its assumptions — replanning is a first-class operation, not an edge case.

### Coordinator
The scheduler. Walks the DAG, dispatches ready tasks, and owns:
- **Retry policy** — bounded attempts with backoff, per task type.
- **Conflict resolution** — when two agents disagree (e.g. Research and Data return different figures for the same fact), the Coordinator routes to a resolution sub-task (re-query with tighter constraints, or escalate to a human) instead of silently picking a winner.
- **Partial completion** — non-critical failed tasks are marked `degraded`; the run continues and the gap is annotated in the final output rather than blocking everything.
- **Escalation** — tasks tagged `requires_human_signoff` pause their branch of the graph and surface in the UI with full context.

### Task Graph / Run State
A LangGraph state graph. Every node transition is checkpointed, so a run can be paused (e.g. waiting on a human) and resumed exactly where it left off, and a replan can graft new nodes onto a live graph without losing prior progress.

---

## Agent catalogue

| Agent | Responsibility | Typical tools (via MCP) |
|---|---|---|
| **Planner** | Goal decomposition, replanning, dependency ordering | Durable memory read |
| **Research Agent** | External/internal document and web retrieval, precedent lookup | Web search, document stores, internal wikis |
| **Data Agent** | Structured queries, ETL, joining across systems | SQL/warehouse APIs, internal REST APIs |
| **Coding Agent** | Ad hoc analysis scripts, transformations, calculations | Sandboxed code execution |
| **Reporting Agent** | Synthesizes agent outputs into a structured, evidenced final artifact | Document/template rendering |

Each agent has a **typed contract**, not free-form text I/O: inputs and outputs are schemas the Coordinator can validate programmatically. Every output carries `result`, `confidence`, and `evidence` fields, so conflict detection and synthesis aren't guessing about what an agent actually found versus assumed.

**Future agents** (roadmap, see below): Compliance Agent (policy-rule checking), Notification Agent (stakeholder updates), Finance Agent (numeric reconciliation against ledgers).

---

## Memory layer

Two tiers:

**Episodic (run-scoped) memory** — the live task graph, intermediate agent outputs, checkpoints. Keyed by run ID. This is what makes pause/resume and replanning possible without re-deriving everything from scratch.

**Semantic (durable) memory** — knowledge that persists across runs: prior findings, resolved conflicts, policy interpretations, vendor histories. Hybrid store: a vector index for chunk-level precedent retrieval, and a lightweight knowledge graph for entity/relationship recall (e.g. "vendor X ↔ prior late-invoice flag ↔ Q1 audit run").

**Promotion policy** — not everything episodic becomes durable. Promotion happens on: human-approved report sections, agent outputs above a confidence threshold, and explicitly tagged "reusable finding" nodes. Promoted facts carry a provenance pointer back to the run that produced them.

**Invalidation policy** — when a new run's finding contradicts a durable fact, the old fact is superseded (not deleted) — the graph keeps both with a supersedes edge, so audit trails survive.

---

## Protocols: A2A and MCP

**A2A (agent-to-agent)** — every message between the Coordinator and an agent, or between two agents when explicitly brokered, uses a fixed envelope: `{task_id, sender, recipient, payload, status, confidence, evidence}`. This is the contract that makes retries, conflict detection, and tracing possible — nothing is passed as loose natural language between agents.

**MCP (tool access)** — agents never hold credentials or call external systems directly. All tool/resource access goes through an MCP Gateway that enforces a per-tool `allowed_principal_types` matrix: each agent type declares which tools it may call, and the gateway checks this on every call, not just at connection time. This mirrors a multi-principal MCP security model (human, agent, service token) with principal type resolved at the registry layer, never trusted as caller-supplied input.

---

## State management

- LangGraph is the state machine: nodes are agent invocations or control steps (plan, dispatch, merge, resolve-conflict, escalate), edges are dependencies.
- Checkpointing happens at every node transition, enabling pause/resume, replay for debugging, and safe replanning mid-run.
- Run state is the single source of truth queried by the UI for the live task-graph view.

---

## Failure handling

| Failure | Handling |
|---|---|
| Tool call fails (API/DB down) | Bounded retry with backoff; task marked `degraded` after limit |
| Two agents produce conflicting facts | Routed to conflict-resolution sub-task: re-query or escalate to human |
| Planner emits an invalid or circular DAG | Validated before dispatch; rejected and reprompted |
| Coding Agent produces unsafe/broken code | Sandboxed execution, strict timeouts, failed artifacts excluded from final report rather than silently included |
| Task exceeds time budget | Timeout with partial-result capture, not a silent hang |
| Human signoff never arrives | TTL on pending tasks; run marked `awaiting_input`, resumable later |
| Durable memory contradiction | Supersede, don't overwrite — keep both facts with provenance |

---

## Observability

Every A2A message and every MCP tool call is traced and keyed by run ID: who called what, with what confidence, what evidence, and what the Coordinator decided when things conflicted or failed. The task-graph UI is a live view over this trace, not a separate logging afterthought. This is deliberately built the same way as a production RAG/agent observability stack: pipeline health, semantic quality of outputs, and reasoning/drift over time are all first-class, queryable dimensions.

---

## Security model

- **Principal types**: human user, orchestrator-internal agent, external service/API client — resolved once at the registry layer from a signed token, never taken as caller-supplied input.
- **Scope enforcement** happens at the MCP Gateway, not inside individual agents.
- **Per-tool access matrix** (`allowed_principal_types`) — e.g. only the Data Agent may call the finance-ledger tool; only human principals can approve a signoff task.
- **No agent holds raw credentials.** All secrets live behind the MCP Gateway.

---

## Tech stack

| Layer | Technology |
|---|---|
| Orchestration / state machine | LangGraph |
| Inter-agent messaging | A2A protocol |
| Tool/resource access | MCP (gateway + per-tool principal matrix) |
| Episodic memory | LangGraph checkpoints, per run ID |
| Semantic memory | Vector store + knowledge graph (Neo4j-style) |
| Code execution | Sandboxed interpreter, timeout-bounded |
| Observability | Structured trace store, queryable by run ID |
| UI | Task-graph visualization + review/signoff console |

---

## Example run, end to end

**Goal:** *"Prepare quarterly audit review."*

1. Planner reads durable memory (prior audit findings, known vendor flags), produces a DAG:
   `gather transaction logs → detect anomalies → cross-reference policy docs → check flagged vendors against precedent → draft report → human signoff`.
2. Coordinator dispatches `gather transaction logs` to Data Agent, `cross-reference policy docs` to Research Agent — both are independent, run in parallel.
3. Data Agent flags three anomalous transactions with medium confidence. Research Agent finds the applicable policy clause. Both write results + evidence back via A2A.
4. Coding Agent runs a variance-detection script on the flagged transactions to quantify severity.
5. One anomaly's vendor conflicts with a durable-memory record (vendor previously cleared, now newly flagged) — Coordinator routes this to conflict resolution: Research Agent re-queries with a tighter date filter, resolves the discrepancy (different fiscal period).
6. Reporting Agent synthesizes everything into a structured draft, explicitly marking the one anomaly still below confidence threshold as `needs human judgment`.
7. Run pauses at the signoff gate. Human reviews, approves two findings, edits one. Approved findings promoted to durable memory with provenance back to this run.

---

## Repository structure

```
enterprise-ai-os/
├── planner/              # goal decomposition, replanning logic
├── coordinator/          # dispatch, retry, conflict resolution, escalation
├── agents/
│   ├── research/
│   ├── data/
│   ├── coding/
│   └── reporting/
├── memory/
│   ├── episodic/         # checkpoint store
│   └── semantic/         # vector index + knowledge graph
├── mcp_gateway/          # tool registry, principal-type enforcement
├── a2a/                  # message envelope schema, transport
├── observability/        # trace store, run inspection
├── ui/                   # task-graph view, review console
└── examples/             # sample runs (e.g. audit review, vendor onboarding)
```

---

## Roadmap

- **Phase 1** — Planner + Coordinator + Research/Data/Reporting agents, single-domain demo (audit review).
- **Phase 2** — Coding Agent as full agent (not just a sandboxed tool), durable memory promotion/invalidation policy hardened.
- **Phase 3** — Additional domain agents (Compliance, Finance reconciliation, Notification), multi-domain goals spanning agent types.
- **Phase 4** — Self-improving Planner: use trace/observability data to refine decomposition quality over time (mirrors a self-improving classifier approach, applied to planning rather than classification).
- **Phase 5** — Multi-tenant hardening: per-tenant memory isolation, full principal-type auth flows, production-grade secrets handling.