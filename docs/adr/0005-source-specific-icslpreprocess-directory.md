---
number: 5
title: Source-specific icslpreprocess directories for concurrent repair
status: accepted
date: 2026-05-14
---

# Context

architecture.md §3 line 162 specifies a single `.icslpreprocess/` directory for agent injection files (CLAUDE.md, icsl_tools.py, config.yaml, counter_examples.md). The implementation uses `.icslpreprocess_{source_id}/` — one directory per source point being repaired.

The repair orchestrator runs multiple source points concurrently (default max_concurrency=5). Each source point gets its own injected CLAUDE.md and .icslpreprocess/ directory, and cleanup removes all injected files after the agent subprocess exits.

# Decision

Use source-specific directories `.icslpreprocess_{source_id}/` instead of a single shared `.icslpreprocess/` directory.

# Rationale

With concurrent source-point repair, a single `.icslpreprocess/` directory would create race conditions:
- Multiple agents writing to the same CLAUDE.md simultaneously
- Cleanup from one agent removing files still needed by another
- Counter-example injection overwriting between concurrent runs

Source-specific directories eliminate all race conditions without requiring file-locking mechanisms.

# Consequences

- The implementation deviates from architecture.md §3 line 162's naming convention
- Each concurrent source gets an isolated working directory, which is safer
- Cleanup is per-source: when one agent exits, only its directory is removed
- The architecture document should be updated to reflect this pattern