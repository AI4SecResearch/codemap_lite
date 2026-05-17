# CONTEXT.md — codemap-lite Domain Language

> Single source of domain vocabulary for this project. All agents, issues, and code should use these terms consistently.

## Glossary

| Term | Definition | Avoid |
|------|-----------|-------|
| **Function Node** | A parsed function/method in the target codebase, stored as Neo4j `(:Function)` | "symbol", "definition" |
| **File Node** | A source file tracked by the scanner, stored as `(:File)` | "module" (ambiguous with source module) |
| **Source Point** | An entry-point function designated by codewiki_lite for repair. Each source point spawns one repair agent. | "entry point" (too generic), "seed" |
| **UnresolvedCall (GAP)** | A call site that static analysis cannot resolve to a single callee. Stored as `(:UnresolvedCall)`. | "indirect call" (subset), "unknown call" |
| **CALLS edge** | A directed relationship `(:Function)-[:CALLS]->(:Function)` with `resolved_by` provenance. | "call edge", "link" |
| **resolved_by** | Provenance tag on CALLS edges: `symbol_table` / `signature` / `dataflow` / `context` / `llm` | "resolution method" |
| **Repair Agent** | A subprocess (claudecode/opencode CLI) that autonomously resolves GAPs for one source point via BFS. | "LLM agent", "fixer" |
| **Gate (门禁)** | The check-complete mechanism: after each agent attempt, verify all reachable GAPs are resolved. | "validation", "check" |
| **RepairLog** | An audit record of one LLM-resolved edge, with reasoning and timestamp. | "fix record", "resolution log" |
| **Counter-example (反例)** | A human-flagged incorrect repair, stored in FeedbackStore and injected into future agent prompts. | "negative example", "bad fix" |
| **Incremental Analysis** | Re-parsing only changed files and cascading invalidation to affected edges/GAPs. | "delta analysis", "partial scan" |

## Architecture Layers

1. **Parsing** — tree-sitter extraction of functions and call sites
2. **Static Analysis** — 3-layer indirect call resolution (signature, dataflow, context)
3. **Graph Storage** — Neo4j with 5 node types and 4 relationship types
4. **Repair Agent** — subprocess LLM that resolves remaining GAPs
5. **REST API** — FastAPI serving graph data and repair status
6. **Frontend** — React + Cytoscape.js for review and visualization

## Key Invariants

- `resolved_by` values are a closed set: adding a new value requires schema + frontend + architecture.md sync
- One repair agent per source point (never shared)
- Gate check is the only path to `SourcePoint.status = "complete"`
- Counter-examples flow: human review → FeedbackStore → CLAUDE.md template injection → next repair run
- All Neo4j writes go through `graph/neo4j_store.py` (never direct driver access from business logic)
