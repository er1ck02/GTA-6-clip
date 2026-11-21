import pytest

from clipstudio.core.errors import Cancelled, StageFailed
from clipstudio.pipeline.project import StateFile, run_stage


def test_cache_skip_and_rerun_on_key_change(tmp_path):
    st = StateFile(tmp_path / "s.json")
    out = tmp_path / "o.txt"
    calls = []

    def fn():
        calls.append(1)
        out.write_text("x")

    assert not run_stage(st, "a", "k1", [out], fn).cached
    assert run_stage(st, "a", "k1", [out], fn).cached
    assert not run_stage(st, "a", "k2", [out], fn).cached
    out.unlink()
    assert not run_stage(st, "a", "k2", [out], fn).cached  # missing output -> rerun
    assert len(calls) == 3
    assert StateFile(tmp_path / "s.json").stage("a").status == "done"  # persisted


def test_failure_is_recorded_not_swallowed(tmp_path):
    st = StateFile(tmp_path / "s.json")

    def boom():
        raise RuntimeError("disk on fire")

    with pytest.raises(StageFailed, match="disk on fire"):
        run_stage(st, "b", "k", [], boom)
    rec = StateFile(tmp_path / "s.json").stage("b")
    assert rec.status == "failed" and "disk on fire" in rec.error


def test_missing_output_is_failure(tmp_path):
    st = StateFile(tmp_path / "s.json")
    with pytest.raises(StageFailed, match="did not produce"):
        run_stage(st, "c", "k", [tmp_path / "never.txt"], lambda: None)


def test_interrupt_marks_stage(tmp_path):
    st = StateFile(tmp_path / "s.json")

    def stop():
        raise KeyboardInterrupt

    with pytest.raises(Cancelled):
        run_stage(st, "d", "k", [], stop)
    assert StateFile(tmp_path / "s.json").stage("d").status == "interrupted"
