"""Tests for CLAUDE.md template generation and prompt building."""
from pathlib import Path

from codemap_lite.agent.claude_md_template import generate_claude_md
from codemap_lite.analysis.prompt_builder import build_repair_prompt


def test_generate_claude_md_contains_tool_instructions():
    result = generate_claude_md(
        source_id="src_001",
        neo4j_config_path=".icslpreprocess/config.yaml",
        counter_examples_path=".icslpreprocess/counter_examples.md",
    )
    assert "icsl_tools.py" in result
    assert "query-reachable" in result
    assert "write-edge" in result
    assert "check-complete" in result


def test_generate_claude_md_contains_termination_conditions():
    result = generate_claude_md(
        source_id="src_001",
        neo4j_config_path=".icslpreprocess/config.yaml",
        counter_examples_path=".icslpreprocess/counter_examples.md",
    )
    # All 5 termination conditions must be present
    assert "系统库" in result or "standard library" in result.lower()
    assert "找不到" in result or "not found" in result.lower()
    assert "环" in result or "cycle" in result.lower() or "递归" in result
    assert "UnresolvedCall" in result or "已处理" in result
    assert "sink" in result or "继续追踪" in result


def test_generate_claude_md_references_counter_examples():
    result = generate_claude_md(
        source_id="src_001",
        neo4j_config_path=".icslpreprocess/config.yaml",
        counter_examples_path=".icslpreprocess/counter_examples.md",
    )
    assert "counter_examples.md" in result


def test_build_repair_prompt_contains_source_id():
    prompt = build_repair_prompt(source_id="src_042")
    assert "src_042" in prompt


def test_build_repair_prompt_contains_workflow_steps():
    prompt = build_repair_prompt(source_id="src_001")
    assert "query-reachable" in prompt
    assert "write-edge" in prompt
