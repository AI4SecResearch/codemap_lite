"""Tests for FeedbackStore — counter example management."""
import tempfile
from pathlib import Path

from codemap_lite.analysis.feedback_store import FeedbackStore, CounterExample


def test_add_counter_example_creates_md_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FeedbackStore(storage_dir=Path(tmpdir))
        example = CounterExample(
            call_context="listener->OnDeviceState(info)",
            wrong_target="SomeUnrelatedClass::OnDeviceState",
            correct_target="CastSessionListenerImpl::OnDeviceState",
            pattern="Listener callback via interface pointer",
        )
        assert store.add(example) is True

        md_path = Path(tmpdir) / "counter_examples.md"
        assert md_path.exists()
        content = md_path.read_text()
        assert "Listener callback" in content
        assert "wrong_target" in content.lower() or "错误目标" in content or "SomeUnrelatedClass" in content


def test_add_multiple_examples():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FeedbackStore(storage_dir=Path(tmpdir))
        for i in range(3):
            assert store.add(CounterExample(
                call_context=f"context_{i}",
                wrong_target=f"Wrong{i}",
                correct_target=f"Correct{i}",
                pattern=f"Pattern {i}",
            )) is True

        examples = store.list_all()
        assert len(examples) == 3


def test_counter_example_deduplication():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FeedbackStore(storage_dir=Path(tmpdir))
        example = CounterExample(
            call_context="ptr->Method()",
            wrong_target="WrongClass::Method",
            correct_target="CorrectClass::Method",
            pattern="Virtual dispatch via base pointer",
        )
        assert store.add(example) is True
        # Adding same pattern again should not duplicate — returns False
        # so the caller (HTTP layer) can surface a "merged" signal to the
        # reviewer (architecture.md §3 反馈机制 step 4).
        assert store.add(CounterExample(
            call_context="other_ptr->Method()",
            wrong_target="WrongClass2::Method",
            correct_target="CorrectClass2::Method",
            pattern="Virtual dispatch via base pointer",
        )) is False

        examples = store.list_all()
        # Same pattern → merged, not duplicated
        assert len(examples) == 1


def test_generate_md_content():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FeedbackStore(storage_dir=Path(tmpdir))
        store.add(CounterExample(
            call_context="callback_(arg)",
            wrong_target="Unrelated::func",
            correct_target="Handler::func",
            pattern="std::function callback",
        ))

        md_path = Path(tmpdir) / "counter_examples.md"
        content = md_path.read_text()
        assert "callback_(arg)" in content
        assert "Handler::func" in content


def test_render_markdown_returns_empty_when_no_examples():
    """architecture.md §3 反馈机制: render_markdown returns empty string
    when no examples exist, so orchestrator falls back to stub."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FeedbackStore(storage_dir=Path(tmpdir))
        assert store.render_markdown() == ""


def test_render_markdown_matches_injection_format():
    """architecture.md §3 反馈机制 step 4: rendered markdown is injected
    into .icslpreprocess/counter_examples.md. Verify it contains the
    structured fields the agent CLAUDE.md references."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FeedbackStore(storage_dir=Path(tmpdir))
        store.add(CounterExample(
            call_context="listener->OnState(info)",
            wrong_target="WrongImpl::OnState",
            correct_target="CorrectImpl::OnState",
            pattern="Listener interface dispatch",
        ))
        md = store.render_markdown()
        # Must contain structured fields the agent can parse
        assert "调用上下文" in md or "call_context" in md.lower()
        assert "错误目标" in md or "wrong_target" in md.lower()
        assert "正确目标" in md or "correct_target" in md.lower()
        assert "listener->OnState(info)" in md
        assert "CorrectImpl::OnState" in md


def test_feedback_store_persists_across_reloads():
    """Counter examples must survive process restart (architecture.md §3
    反馈机制: persistent library, not in-memory only)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store1 = FeedbackStore(storage_dir=Path(tmpdir))
        store1.add(CounterExample(
            call_context="ctx",
            wrong_target="wrong",
            correct_target="correct",
            pattern="persist_test",
        ))
        assert len(store1.list_all()) == 1

        # Simulate process restart — new instance reads from disk
        store2 = FeedbackStore(storage_dir=Path(tmpdir))
        assert len(store2.list_all()) == 1
        assert store2.list_all()[0].pattern == "persist_test"


def test_deduplication_returns_false_for_api_signal():
    """architecture.md §3 反馈机制 + 北极星指标 #5: add() returns False
    when deduplicated so the HTTP layer can signal 'merged' to reviewer."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FeedbackStore(storage_dir=Path(tmpdir))
        ex = CounterExample(
            call_context="a", wrong_target="b",
            correct_target="c", pattern="same_pattern",
        )
        assert store.add(ex) is True  # first time → new
        assert store.add(CounterExample(
            call_context="x", wrong_target="y",
            correct_target="z", pattern="same_pattern",
        )) is False  # same pattern → deduplicated
