"""Auto-update: only a clean fast-forward whose tests pass is taken (temporary git repos, no network)."""
import subprocess
from pathlib import Path

from clipstudio.production import updater


def sh(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repos(tmp: Path, test_body: str = "def test_ok():\n    assert True\n"):
    origin, work = tmp / "origin.git", tmp / "work"
    sh(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp, "clone", "-q", str(origin), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        sh(work, "config", k, v)
    (work / "tests" / "unit").mkdir(parents=True)
    (work / "tests" / "unit" / "test_a.py").write_text(test_body)
    (work / "app.txt").write_text("v1")
    sh(work, "add", "-A")
    sh(work, "commit", "-qm", "v1")
    sh(work, "push", "-q", "origin", "HEAD:main")
    return origin, work


def push_new(tmp: Path, origin: Path, name: str, files: dict):
    other = tmp / name
    sh(tmp, "clone", "-q", str(origin), str(other))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        sh(other, "config", k, v)
    for f, body in files.items():
        (other / f).parent.mkdir(parents=True, exist_ok=True)
        (other / f).write_text(body)
    sh(other, "add", "-A")
    sh(other, "commit", "-qm", name)
    sh(other, "push", "-q", "origin", "HEAD:main")


def test_new_commit_is_found_tested_and_fast_forwarded(tmp_path):
    origin, work = make_repos(tmp_path)
    assert updater.check(repo=work).why_not() == "up to date"
    push_new(tmp_path, origin, "v2", {"app.txt": "v2"})
    chk = updater.check(repo=work)
    assert chk.available and chk.behind == 1 and chk.branch == "main"
    ok, tail = updater.test_commit(chk.remote, repo=work)
    assert ok and "passed" in tail
    assert (work / "app.txt").read_text() == "v1"  # tests ran elsewhere
    assert updater.fast_forward(chk.remote, repo=work)[0]
    assert (work / "app.txt").read_text() == "v2"


def test_failing_tests_and_edited_checkout_block_the_update(tmp_path):
    origin, work = make_repos(tmp_path)
    push_new(tmp_path, origin, "bad", {"tests/unit/test_a.py": "def test_bad():\n    assert False\n"})
    chk = updater.check(repo=work)
    assert chk.available
    assert updater.test_commit(chk.remote, repo=work)[0] is False
    (work / "app.txt").write_text("edited by someone")
    chk = updater.check(repo=work)
    assert not chk.available and "edited" in chk.why_not()
