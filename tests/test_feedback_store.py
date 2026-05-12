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
