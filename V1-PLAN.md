# Enterprise AI OS — High-Level Design

## 1. Purpose & Scope

A multi-agent orchestration system that takes a fuzzy, high-level enterprise goal (e.g. *"Prepare quarterly audit review"*) and autonomously decomposes it into a coordinated set of tasks, executed by specialized agents, producing a verifiable, human-reviewable output.

**Goals for v1 (portfolio scope):**
- Demonstrate planning under ambiguity (goal → task graph)
- Demonstrate multi-agent coordination with real failure handling (not just the happy path)
- Demonstrate a memory layer with actual retrieval/write policy, not a generic vector store
- Demonstrate observability: every decision traceable

**Explicit non-goals for v1:** full generality across arbitrary enterprise domains, production-grade auth/multi-tenancy, five fully-general agents. Depth over breadth — 3 agents built deep beats 5 built shallow.

---

## 2. Actors & Use Case

**Primary actor:** an enterprise user (e.g. compliance lead) who issues a natural-language goal.

**Example flow:** "Prepare quarterly audit review" →
1. Planner decomposes into: gather transaction logs → check for anomalies → cross-reference policy docs → generate report draft → flag items needing human sign-off.
2. Each task routed to the right agent.
3. Results aggregated, conflicts surfaced, report generated.
4. Human reviews and approves/edits.

---

## 3. System Architecture (Component View)

```
┌───────────────────────────────────────────────────────────────────┐
│                         User Interface Layer                      │
│              (goal input, task-graph visualization, review UI)    │
└───────────────────────────────┬───────────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────────┐
│                        Orchestration Layer                        │
│  ┌───────────────┐   ┌─────────────────────┐    ┌───────────────┐ │
│  │ Planner Agent │──▶│ Task Graph / State  │──▶│ Coordinator   │ │
│  │ (decompose,   │   │ (LangGraph nodes +   │   │ (dispatch,    │ │
│  │  replan)      │   │  edges, checkpoints) │   │  retry, merge)│ │
│  └───────────────┘   └─────────────────────┘    └───────┬───────┘ │
└─────────────────────────────────────────────────────────┼─────────┘
                                                          │
        ┌───────────────────────┬───────────────────────┬─┴─────────────────┐
        ▼                       ▼                       ▼                   ▼
┌──────────────┐      ┌──────────────┐        ┌──────────────┐     ┌──────────────┐
│ Research     │      │ Data Agent   │        │ Coding Agent │     │ Reporting    │
│ Agent        │      │ (queries,    │        │ (scripts,    │     │ Agent        │
│ (retrieval,  │      │  ETL, DB/    │        │  analysis    │     │ (synthesis,  │
│  web/doc     │      │  API calls)  │        │  code exec)  │     │  formatting) │
│  search)     │      │              │        │              │     │              │
└──────┬───────┘      └──────┬───────┘        └──────┬───────┘     └──────┬───────┘
       │                     │                       │                    │
       └─────────────────────┴────────────┬──────────┴────────────────────┘
                                          ▼
                          ┌────────────────────────────────┐
                          │         MCP Gateway            │
                          │ (tool access: DBs, APIs, files,│
                          │  code exec sandbox, web)       │
                          └────────────────┬───────────────┘
                                           │
                          ┌────────────────▼───────────────┐
                          │         Memory Layer           │
                          │  ┌─────────────┐ ┌────────────┐│
                          │  │ Episodic /  │ │ Semantic / ││
                          │  │ Run state   │ │ Knowledge  ││
                          │  │ (per-goal)  │ │ (durable)  ││
                          │  └─────────────┘ └────────────┘│
                          └────────────────────────────────┘
```

**A2A note:** agents don't call each other directly. All inter-agent communication is mediated by the Coordinator using an A2A message envelope (task_id, sender, recipient, payload, status). This avoids an N×N mesh of direct agent calls and keeps a single point for tracing, retry, and conflict resolution.

---

## 4. Core Components

### 4.1 Planner Agent
- Input: natural-language goal + relevant memory context.
- Output: a DAG of tasks, each with: `task_id`, `description`, `assigned_agent_type`, `dependencies`, `success_criteria`.
- Must support **replanning**: if a downstream agent reports failure or produces output that invalidates an assumption, Planner is re-invoked with the failure context, not just retried blindly.
- Implementation: LangGraph graph where the Planner is a node that can loop back to itself.

### 4.2 Coordinator (the actual "OS" scheduler)
- Maintains the task graph as LangGraph state.
- Dispatches ready tasks (dependencies satisfied) to the appropriate agent via A2A messages.
- Handles:
  - **Retries** — bounded, with backoff, per task.
  - **Conflicting outputs** — e.g. Research Agent and Data Agent disagree on a figure. Coordinator flags this as a `conflict` node routed to a resolution step (re-query, or escalate to human) rather than silently picking one.
  - **Partial completion** — if a non-critical task fails after retries exhausted, Coordinator marks it `degraded` and continues, annotating the final report rather than blocking the whole run.
  - **Escalation** — tasks tagged `requires_human_signoff` pause the graph and surface to the UI.

### 4.3 Specialized Agents (v1: build 2–3 deep)
Recommended priority for depth: **Research Agent + Data Agent + Reporting Agent** (skip full Coding Agent generality for v1; a narrow code-exec tool used by Data Agent is enough).

Each agent:
- Exposes a narrow, well-typed input/output contract (not free-form text) so the Coordinator can validate outputs programmatically.
- Uses MCP for all external tool/resource access (DB, API, file, web, code sandbox) — never direct credentials.
- Reports a `confidence` and `evidence` field with every output, so downstream synthesis and conflict detection isn't guessing.

### 4.4 Memory Layer
Two tiers, mirroring your Synapt intuition:
- **Run-scoped (episodic) memory**: task graph state, intermediate agent outputs, checkpoints — LangGraph's checkpointing, keyed by goal/run ID. Enables pause/resume and replanning with full context.
- **Durable (semantic) memory**: cross-run knowledge — e.g. "last quarter's audit flagged vendor X for late invoices." Stored in a vector + graph hybrid (a scaled-down version of what you already do at Synapt: Neo4j for entity/relationship recall, vector store for chunk-level precedent).
- **Write policy** is the part that actually needs designing: what gets promoted from episodic → durable (e.g. only human-approved report sections, or agent outputs above a confidence threshold), and how staleness/invalidation is handled (e.g. superseding a prior finding when a new run contradicts it).

### 4.5 MCP Gateway
- Single point for tool/resource access: databases, internal APIs, file storage, code execution sandbox, web search.
- Enforces per-agent `allowed_tools` — same principle as the `allowed_principal_types` matrix you designed for Synapt's MCP security model. Reuse that pattern here: agents are principals, tools declare which agent types may call them.

---

## 5. Data Flow — One Full Cycle

1. User submits goal → stored as new run in episodic memory.
2. Planner queries durable memory for relevant precedent, produces task DAG.
3. Coordinator walks DAG, dispatches ready tasks as A2A messages.
4. Agents pull tools via MCP Gateway, write results + confidence + evidence back to Coordinator.
5. Coordinator checks for conflicts/failures → replan or retry as needed.
6. Reporting Agent synthesizes final output once all required tasks resolve (or are marked degraded).
7. Human review gate → approved sections promoted to durable memory.

---

## 6. Failure Modes to Design (and demo) Explicitly

| Failure | Handling |
|---|---|
| Agent tool call fails (API down) | Bounded retry with backoff; mark task `degraded` after N attempts |
| Two agents produce conflicting facts | Coordinator routes to conflict-resolution step; re-query or escalate |
| Planner produces an invalid/circular DAG | Validate DAG before dispatch; reject and reprompt Planner |
| Coding Agent produces broken/unsafe code | Sandbox execution with strict timeouts; Reporting Agent skips failed artifacts rather than including broken output |
| Long-running task exceeds budget | Timeout with partial-result capture, not silent hang |
| Human never responds to signoff | TTL on pending human tasks; run marked `awaiting_input`, resumable |

---

## 7. Tech Stack Mapping

| Layer | Tech |
|---|---|
| Orchestration / state machine | LangGraph (nodes, edges, checkpointing) |
| Inter-agent messaging | A2A protocol (task envelopes, status) |
| Tool/resource access | MCP (gateway + per-tool `allowed_principal_types`) |
| Memory — episodic | LangGraph checkpoints, keyed by run ID |
| Memory — semantic | Vector store + lightweight graph (Neo4j-style) for durable knowledge |
| Agents | LLM-backed nodes with typed I/O contracts, per-agent system prompts |
| Observability | Trace every A2A message + tool call, keyed by run ID (borrow your existing eval/observability instincts from Synapt) |

---

## 8. Suggested Build Order

1. LangGraph skeleton: Planner → Coordinator → one dummy agent → memory checkpoint. Get the loop working end to end.
2. Add Research + Data agents with real MCP tool calls.
3. Add conflict detection and retry/degradation logic — this is the part worth demoing live.
4. Add Reporting Agent + human signoff gate.
5. Add durable memory promotion policy.
6. (Stretch) Add Coding Agent as a narrow sandboxed tool rather than a full agent.