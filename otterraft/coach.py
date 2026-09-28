"""Reflection Coach: a periodic Claude run that reads the recent task history, finds patterns
that repeat, and proposes small, durable fixes — an addition to CLAUDE.md or a skill.

Rules (borrowed from Paperclip's reflection coach):
- every proposal cites the tasks that justify it; no evidence, no proposal
- proposals are small (CLAUDE.md grows at most ~20% per proposal, skills stay under 15 KB)
- discovery and application are separate: the coach only proposes, it runs with read-only
  tools, and nothing is written until you approve it in the dashboard
- every applied change keeps a backup and can be rolled back"""
import json
import re
import shutil
import time
from pathlib import Path

COACH_SYSTEM = """You are the Reflection Coach of OtterRaft, an AI task orchestrator. You never do the tasks
yourself. You read the evidence of recent tasks and propose the smallest durable changes that
would have made the agents succeed with less help from the user.

Read these files in the working directory first:
- evidence.json: recent tasks (prompt, outcome, report, verify output, tool errors, user
  feedback, retries). Rejections, failed verifies and repeated tool errors matter most.
- targets.json: the files you may propose changes to, with paths to copies of their current text.

Allowed proposal kinds:
- CLAUDE.md addition: target "global" (all projects) or "project:<repo path from targets.json>",
  action "append", content = a short markdown section to add.
- Skill: target "skill:<slug>" (lowercase letters, digits, dashes), action "create" for a new skill
  or "replace" for an existing one, content = the full SKILL.md, starting with YAML frontmatter
  (--- name: <slug> / description: <when to use it> ---).

Hard rules:
- Every proposal must list evidence task ids from evidence.json that show the problem.
  Patterns seen in only one task need a very clear cause; prefer patterns that repeat.
- Be specific: exact commands, file names, conventions. No generic advice ("write tests").
- Keep it small: a CLAUDE.md addition at most 20% of the file's current size (or 1500
  characters for a small file); a skill at most 15 KB. Split big ideas.
- Do not repeat what the target file already says.
- Propose nothing rather than something weak. Zero proposals is a valid answer.

End your final message with exactly one block:
```coach-proposals
{"summary": "what you looked at and found", "proposals": [
  {"title": "...", "target": "global | project:<path> | skill:<slug>", "action": "append | create | replace",
   "rationale": "the pattern and why this fixes it", "evidence": [12, 15], "content": "..."}
]}
```"""

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
BLOCK_RE = re.compile(r"```coach-proposals\s*(\{.*?\})\s*```", re.S)
SKILL_MAX = 15_000
FINISHED = ("done", "review", "failed", "needs_input", "cancelled")


class Coach:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self._last_check = 0

    # -- scheduling ------------------------------------------------------------
    def last_run_at(self):
        row = self.db.one("SELECT MAX(created_at) t FROM tasks WHERE kind='reflection'")
        return row["t"] or 0

    def evidence_tasks(self, since):
        return self.db.query(
            f"SELECT * FROM tasks WHERE kind='task' AND status IN ({','.join('?' * len(FINISHED))}) "
            "AND finished_at>? ORDER BY id DESC LIMIT ?",
            (*FINISHED, since, self.cfg["learning"].get("coach_max_tasks", 40)))

    def due(self, now=None):
        """True when the periodic run should start. Checked at most every 10 minutes."""
        now = now or time.time()
        every = self.cfg["learning"].get("coach_every_days", 7)
        if not every or now - self._last_check < 600:
            return False
        self._last_check = now
        if self.db.one("SELECT id FROM tasks WHERE kind='reflection' AND status IN ('queued','running')"):
            return False
        last = self.last_run_at()
        if now - last < every * 86400:
            return False
        return len(self.evidence_tasks(last)) >= self.cfg["learning"].get("coach_min_tasks", 5)

    # -- building the run ----------------------------------------------------
    def targets(self, tasks):
        shared = Path(self.cfg["shared_config_dir"])
        repos = sorted({t.get("base_repo") or t.get("workdir") for t in tasks
                        if (t.get("base_repo") or t.get("workdir"))
                        and "/workspaces/task-" not in (t.get("workdir") or "")})
        out = {"global": {"path": str(shared / "CLAUDE.md")}}
        for r in repos:
            out[f"project:{r}"] = {"path": str(Path(r) / "CLAUDE.md")}
        skills_dir = shared / "skills"
        skills = sorted(p.parent.name for p in skills_dir.glob("*/SKILL.md")) if skills_dir.is_dir() else []
        return out, skills

    def prepare_run(self, since=None):
        """Write evidence into a fresh directory. Returns (workdir, n_tasks) or (None, 0)."""
        since = self.last_run_at() if since is None else since
        tasks = self.evidence_tasks(since)
        if not tasks:
            return None, 0
        run_dir = Path(self.cfg["data_dir"]) / "reflection" / time.strftime("run-%Y%m%d-%H%M%S")
        (run_dir / "current").mkdir(parents=True, exist_ok=True)
        targets, skills = self.targets(tasks)
        for i, (key, t) in enumerate(targets.items()):
            src = Path(t["path"])
            copy = run_dir / "current" / f"{i}-CLAUDE.md"
            copy.write_text(src.read_text(encoding="utf-8") if src.exists() else "", encoding="utf-8")
            t.update(copy=str(copy.relative_to(run_dir)), exists=src.exists(),
                     size=src.stat().st_size if src.exists() else 0)
        skill_root = Path(self.cfg["shared_config_dir"]) / "skills"
        (run_dir / "targets.json").write_text(json.dumps(
            {"claude_md": targets, "existing_skills": skills, "skills_dir": str(skill_root)},
            indent=2, ensure_ascii=False), encoding="utf-8")
        (run_dir / "evidence.json").write_text(json.dumps(
            [self._evidence(t) for t in tasks], indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return str(run_dir), len(tasks)

    def _evidence(self, t):
        events = self.db.events(t["id"])
        tool_errors = [e["data"].get("text", "")[:300] for e in events if e["kind"] == "tool_error"][:6]
        interruptions = [e["kind"] for e in events if e["kind"] in
                         ("account_limit", "retry_later", "session_reset", "escalated", "stalled")]
        report = t.get("report") or {}
        verify = t.get("verify") or {}
        return {
            "id": t["id"], "title": t.get("title"), "prompt": (t.get("prompt") or "")[:1500],
            "repo": t.get("base_repo") or t.get("workdir"), "agent": t.get("agent"),
            "category": t.get("category"), "status": t.get("status"),
            "user_rating": {1: "accepted", -1: "rejected"}.get(t.get("rating")),
            "user_feedback": t.get("feedback"),
            "summary": report.get("summary"), "notes": report.get("notes"),
            "questions_asked": report.get("questions"),
            "verify": {"cmd": verify.get("cmd"), "ok": verify.get("ok"),
                       "output_tail": (verify.get("output") or "")[-800:]} if verify else None,
            "error": (t.get("error") or "")[:500] or None, "tool_errors": tool_errors,
            "interruptions": interruptions, "turns": t.get("num_turns"), "attempts": t.get("attempts"),
        }

    # -- ingesting the coach's answer -------------------------------------------
    def ingest(self, task_id, text, run_dir):
        """Validate and store the proposals from a finished coach run. Returns (summary, n_stored)."""
        m = list(BLOCK_RE.finditer(text or ""))
        if not m:
            return "The coach returned no proposals block.", 0
        try:
            data = json.loads(m[-1].group(1))
        except ValueError as e:
            return f"Unreadable proposals block: {e}", 0
        evidence_ids = {e["id"] for e in json.loads((Path(run_dir) / "evidence.json").read_text())}
        targets = json.loads((Path(run_dir) / "targets.json").read_text())["claude_md"]
        stored = 0
        for p in (data.get("proposals") or [])[:10]:
            try:
                path, kind = self.resolve(p, targets)
                ev = [int(x) for x in p.get("evidence") or [] if int(x) in evidence_ids]
                if not ev:
                    raise ValueError("no valid evidence task ids")
                self.check_size(kind, p.get("action"), p.get("content") or "", path)
                status, err = "pending", None
            except (ValueError, TypeError) as e:
                path, kind, ev, status, err = None, None, p.get("evidence") or [], "invalid", str(e)
            self.db.execute(
                "INSERT INTO proposals (task_id, title, target, target_path, kind, action, rationale, "
                "evidence, content, status, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, (p.get("title") or "")[:200], p.get("target"), path, kind, p.get("action"),
                 p.get("rationale"), json.dumps(ev), p.get("content"), status, err, time.time()))
            stored += status == "pending"
        return data.get("summary") or "", stored

    def resolve(self, p, targets):
        target, action = p.get("target") or "", p.get("action")
        if target == "global" or target.startswith("project:"):
            if target not in targets:
                raise ValueError(f"unknown target {target}")
            if action != "append":
                raise ValueError("CLAUDE.md proposals must use action 'append'")
            return targets[target]["path"], "claude_md"
        if target.startswith("skill:"):
            slug = target[6:]
            if not SLUG_RE.match(slug):
                raise ValueError(f"bad skill slug {slug!r}")
            path = Path(self.cfg["shared_config_dir"]) / "skills" / slug / "SKILL.md"
            if action not in ("create", "replace") or (action == "create") == path.exists():
                raise ValueError(f"action {action!r} does not fit: skill {'exists' if path.exists() else 'is new'}")
            return str(path), "skill"
        raise ValueError(f"unknown target {target!r}")

    @staticmethod
    def check_size(kind, action, content, path):
        if not content.strip():
            raise ValueError("empty content")
        if kind == "claude_md":
            size = Path(path).stat().st_size if Path(path).exists() else 0
            limit = max(1500, int(size * 0.2))
            if len(content.encode()) > limit:
                raise ValueError(f"CLAUDE.md addition is {len(content.encode())} bytes, limit {limit}")
        else:
            if len(content.encode()) > SKILL_MAX:
                raise ValueError(f"skill is {len(content.encode())} bytes, limit {SKILL_MAX}")
            if not re.match(r"^---\s*\n(?:.*\n)*?name:\s*\S+", content):
                raise ValueError("skill must start with YAML frontmatter containing name:")

    # -- applying (only ever from an explicit user action) -------------------------
    def apply(self, proposal_id):
        p = self.db.one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        if not p or p["status"] != "pending":
            raise ValueError("proposal is not pending")
        path = Path(p["target_path"])
        self.check_size(p["kind"], p["action"], p["content"], path)  # the file may have changed since
        backup = None
        if path.exists():
            bdir = Path(self.cfg["data_dir"]) / "backups"
            bdir.mkdir(parents=True, exist_ok=True)
            backup = bdir / f"{int(time.time())}-p{p['id']}-{path.parent.name}-{path.name}"
            shutil.copy2(path, backup)
        path.parent.mkdir(parents=True, exist_ok=True)
        if p["kind"] == "claude_md":
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            sep = "" if not old or old.endswith("\n\n") else ("\n" if old.endswith("\n") else "\n\n")
            path.write_text(old + sep + p["content"].strip() + "\n", encoding="utf-8")
        else:
            path.write_text(p["content"].strip() + "\n", encoding="utf-8")
        self.db.execute("UPDATE proposals SET status='applied', applied_at=?, backup_path=? WHERE id=?",
                        (time.time(), str(backup) if backup else None, p["id"]))

    def reject(self, proposal_id):
        self.db.execute("UPDATE proposals SET status='rejected' WHERE id=? AND status='pending'", (proposal_id,))

    def rollback(self, proposal_id):
        p = self.db.one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        if not p or p["status"] != "applied":
            raise ValueError("proposal is not applied")
        path = Path(p["target_path"])
        if p["backup_path"]:
            shutil.copy2(p["backup_path"], path)
        elif path.exists():
            path.unlink()  # the proposal created this file
            if p["kind"] == "skill" and not any(path.parent.iterdir()):
                path.parent.rmdir()
        self.db.execute("UPDATE proposals SET status='rolled_back' WHERE id=?", (p["id"],))
