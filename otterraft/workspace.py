"""Isolated git worktrees: each task works on its own branch in its own directory, so several
tasks can run on one repo at once and nothing touches your checkout until you merge."""
import os
import re
import shutil
import subprocess
from pathlib import Path


JUNK = ["**/__pycache__/**", "**/*.pyc", "**/node_modules/**", "**/.venv/**", "**/venv/**",
        "**/.pytest_cache/**", "**/.mypy_cache/**", "**/.ruff_cache/**", "**/.DS_Store", "**/.tox/**"]


# Instruction files you may have edited but not committed yet (e.g. a Reflection Coach change).
# A worktree starts from HEAD, so these are copied in, and never committed on the task branch.
OVERLAY = ["CLAUDE.md", "CLAUDE.local.md"]


class WorkspaceError(Exception):
    pass


def git(args, cwd, timeout=120, env=None):
    # quotepath=off: file names with accents come back as text, not "t\341\273\207p.txt".
    p = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", env={**os.environ, **(env or {})})
    if p.returncode:
        raise WorkspaceError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[:500]}")
    return p.stdout.strip()


def repo_root(path):
    """Top of the git repo containing path, or None (not a repo, or no commit yet)."""
    try:
        root = git(["rev-parse", "--show-toplevel"], path, timeout=20)
        git(["rev-parse", "--verify", "HEAD"], path, timeout=20)
        return root
    except (WorkspaceError, OSError, subprocess.TimeoutExpired):
        return None


def prepare(cfg, task):
    """Create the task's worktree. Returns fields to store on the task, or None when the task
    should run in place (worktrees off, or the workdir is not a git repo)."""
    ws = cfg["workspace"]
    root = repo_root(task["workdir"]) if ws.get("use_worktrees") and task.get("workdir") else None
    if not root:
        return None
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(root).name)[:40]
    path = Path(cfg["data_dir"]) / "worktrees" / f"{slug}-task-{task['id']}"
    branch = f"{ws.get('branch_prefix', 'otterraft/task-')}{task['id']}"
    base_ref = git(["rev-parse", "HEAD"], root)
    base_branch = git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        git(["worktree", "add", "-b", branch, str(path), base_ref], root)
    except WorkspaceError:
        git(["worktree", "add", str(path), branch], root)  # branch left over from an earlier run
    for name in OVERLAY:
        src = Path(root) / name
        if src.exists() and git(["status", "--porcelain", "--", name], root):
            shutil.copy2(src, path / name)
    exec_dir = path / Path(task["workdir"]).resolve().relative_to(Path(root).resolve())
    if ws.get("setup_cmd"):
        p = subprocess.run(ws["setup_cmd"], shell=True, cwd=exec_dir, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=1800,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if p.returncode:
            raise WorkspaceError(f"setup_cmd failed:\n{(p.stdout + p.stderr)[-1500:]}")
    return {"exec_dir": str(exec_dir), "branch": branch, "base_repo": root,
            "base_ref": base_ref, "base_branch": base_branch}


def commit(task):
    """Commit whatever the agent left uncommitted, then describe the branch vs its base.
    Returns (changed_files, diffstat)."""
    top = git(["rev-parse", "--show-toplevel"], task["exec_dir"])
    # Build and test leftovers never belong in the task's commit, even without a .gitignore.
    overlay = [n for n in OVERLAY if (Path(task["base_repo"]) / n).exists()
               and git(["status", "--porcelain", "--", n], task["base_repo"])] if task.get("base_repo") else []
    git(["add", "-A", "--", ".", *(f":(exclude,glob){p}" for p in JUNK),
         *(f":(exclude,top){n}" for n in overlay)], top)
    if git(["diff", "--cached", "--name-only"], top):
        git(["commit", "-q", "--no-verify", "-m", f"otterraft: task #{task['id']} {task.get('title') or ''}"[:200]],
            top, env=_identity_env(top))
    files = git(["diff", "--name-only", task["base_ref"], "HEAD"], top).splitlines()
    stat = git(["diff", "--shortstat", task["base_ref"], "HEAD"], top)
    return files[:300], stat


def diff(task, max_chars=200_000):
    top = git(["rev-parse", "--show-toplevel"], task["exec_dir"])
    out = git(["diff", "--stat", "--patch", task["base_ref"], "HEAD"], top, timeout=60)
    return out if len(out) <= max_chars else out[:max_chars] + "\n… (diff truncated)"


def merge(task):
    """Merge the task branch into the branch it started from, in your own checkout.
    Refuses when the checkout moved to another branch or has uncommitted tracked changes."""
    root = task["base_repo"]
    current = git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if current != task["base_branch"]:
        raise WorkspaceError(f"your checkout is on '{current}', expected '{task['base_branch']}'")
    if git(["status", "--porcelain", "--untracked-files=no"], root):
        raise WorkspaceError("your checkout has uncommitted changes; commit or stash them first")
    try:
        git(["merge", "--no-ff", "--no-edit", task["branch"]], root, env=_identity_env(root))
    except WorkspaceError as e:
        try:
            git(["merge", "--abort"], root)
        except WorkspaceError:
            pass
        raise WorkspaceError(f"merge failed, nothing changed: {e}")
    return git(["rev-parse", "--short", "HEAD"], root)


def remove(task, delete_branch):
    top = Path(task["exec_dir"])
    wt = git(["rev-parse", "--show-toplevel"], top) if top.exists() else None
    if wt:
        git(["worktree", "remove", "--force", wt], task["base_repo"])
    git(["worktree", "prune"], task["base_repo"])
    if delete_branch:
        git(["branch", "-D", task["branch"]], task["base_repo"])


def _identity_env(cwd):
    """Commits and merge commits need a name and email; a fresh machine may have none."""
    try:
        if git(["config", "user.email"], cwd, timeout=10):
            return {}
    except WorkspaceError:
        pass
    return {"GIT_AUTHOR_NAME": "OtterRaft", "GIT_AUTHOR_EMAIL": "otterraft@localhost",
            "GIT_COMMITTER_NAME": "OtterRaft", "GIT_COMMITTER_EMAIL": "otterraft@localhost"}
