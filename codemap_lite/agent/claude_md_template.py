"""CLAUDE.md template generation for repair agents."""
from __future__ import annotations


def generate_claude_md(
    source_id: str,
    neo4j_config_path: str = ".icslpreprocess/config.yaml",
    counter_examples_path: str = ".icslpreprocess/counter_examples.md",
) -> str:
    """Generate CLAUDE.md content for a repair agent working on a specific source point."""
    return f"""# Repair Agent — Source Point {source_id}

## Role

You are a code analysis agent. Your task is to resolve indirect function calls (GAPs)
reachable from source point `{source_id}`. You will read source code, analyze call contexts,
and write resolved edges to the call graph.

## Tools

Use `.icslpreprocess/icsl_tools.py` for all graph operations:

- `python .icslpreprocess/icsl_tools.py query-reachable --source {source_id}`
  → Returns the reachable subgraph (nodes, edges, unresolved calls)

- `python .icslpreprocess/icsl_tools.py write-edge --caller <id> --callee <id> --call-type <type> --call-file <file> --call-line <line>`
  → Writes a CALLS edge + RepairLog, deletes the UnresolvedCall

- `python .icslpreprocess/icsl_tools.py check-complete --source {source_id}`
  → Checks if all reachable GAPs are resolved

## Configuration

Neo4j connection: `{neo4j_config_path}`

## Counter Examples (反例库)

Review `{counter_examples_path}` before making repair decisions.
These are patterns where previous repairs were incorrect — avoid repeating them.

## Workflow

1. Run `query-reachable --source {source_id}` to get the current state
2. For each UnresolvedCall in the result:
   a. Read the source file at the call location
   b. Analyze the call context (variable type, assignment history, candidates)
   c. Determine the correct call target(s)
   d. Run `write-edge` for each resolved target
3. Run `query-reachable` again — new UnresolvedCalls may appear (newly reachable)
   - If new ones exist → repeat from step 2
   - If none → you are done

## Termination Conditions

Stop processing an UnresolvedCall when ANY of these apply:
1. 到达系统库/标准库函数（无源码可读）— The target is a system/standard library function with no source
2. 搜索整个代码库找不到函数实现 — Cannot find the function implementation anywhere in the codebase
3. 调用链形成环（递归检测）— A cycle is detected in the call chain
4. 所有可达 UnresolvedCall 已处理 — All reachable UnresolvedCalls have been processed
5. 到达 sink 点后继续追踪 — After reaching a sink, continue tracing to discover all reachable paths

## Important Rules

- Before writing an edge, check if it already exists (skip if so)
- Use the candidates list as a REFERENCE, not as the definitive answer
- You may discover targets not in the candidates list — that is expected
- Read actual source code to confirm your analysis
"""
