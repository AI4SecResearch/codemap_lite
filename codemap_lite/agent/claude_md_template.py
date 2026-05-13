"""CLAUDE.md template generation for repair agents."""
from __future__ import annotations


def generate_claude_md(
    source_id: str,
    neo4j_config_path: str = ".icslpreprocess/config.yaml",
    counter_examples_path: str = ".icslpreprocess/counter_examples.md",
) -> str:
    """Generate CLAUDE.md content for a repair agent working on a specific source point."""
    # Derive the icsl_tools.py path from the config path's parent directory
    # (e.g. ".icslpreprocess_src_001/config.yaml" → ".icslpreprocess_src_001/icsl_tools.py")
    from pathlib import PurePosixPath
    icsl_dir = str(PurePosixPath(neo4j_config_path).parent)
    icsl_tools = f"{icsl_dir}/icsl_tools.py"

    return f"""# Repair Agent — Source Point {source_id}

## Role

You are a code analysis agent. Your task is to resolve indirect function calls (GAPs)
reachable from source point `{source_id}`. You will read source code, analyze call contexts,
and write resolved edges to the call graph.

## Tools

Use `{icsl_tools}` for all graph operations:

- `python {icsl_tools} query-reachable --source {source_id}`
  → Returns the reachable subgraph (nodes, edges, unresolved calls)

- `python {icsl_tools} write-edge --caller <id> --callee <id> --call-type <type> --call-file <file> --call-line <line> --llm-response <raw> --reasoning-summary <one-sentence justification>`
  → Writes a CALLS edge + RepairLog, deletes the UnresolvedCall.
  → **MANDATORY**: You MUST pass both `--llm-response` and `--reasoning-summary` on every `write-edge` call. These populate `RepairLogNode` fields surfaced to human reviewers in the call-graph UI. Omitting them degrades the audit trail.

- `python {icsl_tools} check-complete --source {source_id}`
  → Checks if all reachable GAPs are resolved

## Configuration

Neo4j connection: `{neo4j_config_path}`

## Counter Examples (反例库)

**MANDATORY**: Before resolving any UnresolvedCall, read `{counter_examples_path}`.
Each entry describes a pattern where a previous repair was incorrect.

For every edge you are about to write:
1. Check if the call context matches any counter-example pattern
2. If it matches → do NOT write that edge; reconsider your analysis
3. If unsure → err on the side of caution and skip

These rules override your own analysis. A repeated mistake wastes reviewer time
and burns retry budget.

## Workflow

1. Run `query-reachable --source {source_id}` to get the current state
2. For each UnresolvedCall in the result:
   a. Read the source file at the call location
   b. **Check `{counter_examples_path}`** — if the call context matches any counter-example pattern, skip this UC (do NOT write that edge)
   c. Analyze the call context (variable type, assignment history, candidates)
   d. Determine the correct call target(s)
   e. Run `write-edge` for each resolved target (you MUST include `--llm-response` + `--reasoning-summary`)
3. Run `query-reachable` again — new UnresolvedCalls may appear (newly reachable)
   - If new ones exist → repeat from step 2
   - If none → you are done

## Reasoning capture

Every `write-edge` call you make here is an **llm-resolution** — downstream reviewers audit these edges in the call-graph UI. On each invocation you MUST pass:

- `--reasoning-summary "<one sentence>"` — a concise human-readable justification, e.g. `"ptr->handle() dispatches to DerivedHandler::handle based on the ctor at line 24"`. Keep it ≤200 characters.
- `--llm-response "<excerpt>"` — a short excerpt of your analysis (the key quote or conclusion). Truncate aggressively; shells don't like multi-kilobyte args. Pick what a reviewer would want to see.

Do NOT leave these flags empty. Empty reasoning shows up in the UI as "No reasoning summary recorded", which forces reviewers to fall back to log files and wastes their time.

## Termination Conditions

Stop processing an UnresolvedCall when ANY of these apply:
1. 到达系统库/标准库函数（无源码可读）— The target is a system/standard library function with no source
2. 搜索整个代码库找不到函数实现 — Cannot find the function implementation anywhere in the codebase
3. 调用链形成环（递归检测）— A cycle is detected in the call chain
4. 所有可达 UnresolvedCall 已处理 — All reachable UnresolvedCalls have been processed

**Important**: Reaching a sink point is NOT a stop condition. After reaching a sink, CONTINUE tracing to discover all reachable paths beyond it.

## Important Rules

- Before writing an edge, check if it already exists (skip if so)
- Use the candidates list as a REFERENCE, not as the definitive answer
- You may discover targets not in the candidates list — that is expected
- Read actual source code to confirm your analysis
"""
