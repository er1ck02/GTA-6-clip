"""Auto-update: while production runs, pick up new commits on the checkout's branch and restart onto them.

Only a fast-forward of a clean checkout is taken (local commits or edited files mean someone is working here, so
nothing is touched). The new code's unit tests run first in a throwaway git worktree; only if they pass does
production stop quickly (the clip being edited finishes, waiting moments are saved and resumed), the checkout
fast-forward, and the server start again on the new code.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clipstudio.core.logging import get_logger

log = get_logger("production.update")
REPO = Path(__file__).resolve().parents[2]


def git(*args: str, cwd: Path = REPO, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


@dataclass
class UpdateCheck:
    branch: str = ""
    local: str = ""
    remote: str = ""
    behind: int = 0
    fast_forward: bool = False
    clean: bool = False
    error: str = ""
    running: str = ""   # the commit the server was started on ("" = not known)

    @property
    def stale(self) -> bool:
        """The checkout moved (a commit made right here, or a pull by hand) but the server still runs older code."""
        return bool(self.running and self.local and self.running != self.local)

    @property
    def target(self) -> str:
        return self.remote if self.behind > 0 else self.local

    @property
    def available(self) -> bool:
        if self.error or not self.clean:
            return False
        return (self.behind > 0 and self.fast_forward) or (self.behind == 0 and self.stale)

    def why_not(self) -> str:
        if self.error:
            return self.error
        if self.behind == 0 and not self.stale:
            return "up to date"
        if not self.clean:
            return "tracked files are edited in the checkout; not touching it"
        if not self.fast_forward:
            return "the checkout has local commits; not touching it"
        return ""


def head(repo: Path = REPO) -> str:
    try:
        return git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check(remote: str = "origin", repo: Path = REPO, running: str = "") -> UpdateCheck:
    """Fetch the current branch from ``remote`` and say whether new code is waiting: a clean fast-forward, or a
    checkout that already moved past the commit the server is ``running``."""
    r = UpdateCheck(running=running)
    try:
        cp = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        r.branch = cp.stdout.strip()
        if cp.returncode or not r.branch or r.branch == "HEAD":
            r.error = "not on a branch"
            return r
        cp = git("fetch", "--quiet", remote, r.branch, cwd=repo, timeout=180)
        if cp.returncode:
            r.error = f"fetch failed: {cp.stderr.strip()[-200:]}"
            return r
        r.local = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        r.remote = git("rev-parse", "FETCH_HEAD", cwd=repo).stdout.strip()
        r.behind = int(git("rev-list", "--count", "HEAD..FETCH_HEAD", cwd=repo).stdout.strip() or 0)
        r.fast_forward = git("merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD", cwd=repo).returncode == 0
        r.clean = git("status", "--porcelain", "--untracked-files=no", cwd=repo).stdout.strip() == ""
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        r.error = f"git: {e}"
    return r


def test_commit(sha: str, repo: Path = REPO, timeout: float = 900) -> tuple[bool, str]:
    """Run the unit tests of ``sha`` in a temporary worktree (the live checkout is untouched)."""
    tmp = Path(tempfile.mkdtemp(prefix="clipstudio-update-"))
    wt = tmp / "wt"
    try:
        cp = git("worktree", "add", "--detach", str(wt), sha, cwd=repo)
        if cp.returncode:
            return False, f"worktree failed: {cp.stderr.strip()[-200:]}"
        cp = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/unit"],
                            cwd=wt, capture_output=True, text=True, timeout=timeout)
        tail = (cp.stdout.strip().splitlines() or [""])[-1]
        return cp.returncode == 0, tail
    except subprocess.TimeoutExpired:
        return False, "tests timed out"
    except OSError as e:
        return False, str(e)
    finally:
        git("worktree", "remove", "--force", str(wt), cwd=repo)
        shutil.rmtree(tmp, ignore_errors=True)


def fast_forward(sha: str, repo: Path = REPO) -> tuple[bool, str]:
    cp = git("merge", "--ff-only", sha, cwd=repo)
    return cp.returncode == 0, (cp.stdout + cp.stderr).strip()[-300:]
