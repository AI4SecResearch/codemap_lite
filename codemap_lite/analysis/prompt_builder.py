"""Prompt builder for repair agent subprocess invocations."""
from __future__ import annotations


def build_repair_prompt(source_id: str) -> str:
    """Build the prompt passed to the CLI agent for repairing a specific source point."""
    return f"""You are repairing indirect calls for source point {source_id}.

Follow these steps:

1. Run: python .icslpreprocess/icsl_tools.py query-reachable --source {source_id}
   This returns the reachable subgraph with all UnresolvedCalls.

2. For each UnresolvedCall:
   - Read the source file at the call location
   - Analyze variable types, assignments, and context
   - Identify the correct call target(s)
   - Run: python .icslpreprocess/icsl_tools.py write-edge --caller <caller_id> --callee <callee_id> --call-type <indirect|virtual> --call-file <file> --call-line <line> --llm-response "<your analysis excerpt>" --reasoning-summary "<one-sentence justification>"

3. After processing all current UnresolvedCalls, run query-reachable again.
   New UnresolvedCalls may appear as newly reachable nodes are discovered.
   Repeat until no new UnresolvedCalls remain.

4. When done, the orchestrator will run check-complete to verify.

Remember:
- Check counter_examples.md before deciding targets
- Skip edges that already exist
- Always pass --llm-response and --reasoning-summary on every write-edge call
- Stop when you reach system/standard library functions (no source available)
- Stop when you cannot find the implementation in the codebase
- Detect and break cycles in the call chain
"""
