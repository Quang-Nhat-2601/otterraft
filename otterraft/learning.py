"""Self-improvement loop: lessons extracted after each task are fed back into future prompts,
permission denials turn into allow-list suggestions, and user feedback re-weights lessons."""
import json
import re
import time

REFLECT_SYSTEM = """You review a finished AI coding task and extract reusable lessons that would help
an autonomous agent work more independently next time in the same project.
Only include lessons that are specific and actionable (commands that work, project conventions,
pitfalls, what the user rejected and why). No generic advice. Reply with JSON only:
{"lessons": [{"text": "...", "scope": "project" | "global", "tags": ["..."]}]}  (0-4 lessons)"""

_WORD = re.compile(r"[\wÀ-ỹ]{3,}", re.U)


def _words(s):
    return {w.lower() for w in _WORD.findall(s or "")}


# Commands that must never be allow-listed wholesale, however often they get blocked.
DANGEROUS = {"rm", "sudo", "su", "dd", "mkfs", "chmod", "chown", "curl", "wget", "ssh", "scp",
             "rsync", "kill", "pkill", "killall", "shutdown", "reboot", "eval", "sh", "bash", "zsh",
             "python", "python3", "node", "docker", "kubectl", "terraform", "aws", "gcloud", "az"}
DANGEROUS_SUB = {("git", "push"), ("git", "reset"), ("git", "clean"), ("npm", "publish")}


def suggest_rule(tool, inp):
    """Turn a blocked tool call into the narrowest useful allow rule, or None when the call is
    too dangerous to allow in bulk (those stay under auto mode's per-call judgement)."""
    if tool != "Bash":
        return tool
    cmd = (inp.get("command") or "").strip()
    if not cmd or any(c in cmd for c in ("|", ";", "&&", "||", "`", "$(", ">")):
        return None  # compound commands can smuggle anything behind an innocent prefix
    words = cmd.split()
    if words[0] in DANGEROUS or tuple(words[:2]) in DANGEROUS_SUB:
        return None
    prefix = " ".join(words[:2]) if len(words) > 1 and not words[1].startswith("-") else words[0]
    return f"Bash({prefix}:*)"


class Learner:
    def __init__(self, cfg, db, local):
        self.cfg = cfg
        self.db = db
        self.local = local

    # -- feeding lessons into prompts ----------------------------------------
    def relevant_lessons(self, prompt, workdir):
        if not self.cfg["learning"]["enabled"]:
            return []
        rows = self.db.query(
            "SELECT * FROM lessons WHERE enabled=1 AND pending=0 AND (scope='global' OR scope=?)",
            (workdir or "",))
        q = _words(prompt)
        scored = []
        for r in rows:
            overlap = len(q & (_words(r["text"]) | set(r.get("tags") or [])))
            project_bonus = 1.5 if r["scope"] == (workdir or "") else 0
            scored.append((overlap + project_bonus + r["score"] * 0.5, r))
        scored.sort(key=lambda x: -x[0])
        picked = [r for _, r in scored[: self.cfg["learning"]["max_lessons_in_prompt"]]]
        for r in picked:
            self.db.execute("UPDATE lessons SET uses=uses+1 WHERE id=?", (r["id"],))
        return picked

    @staticmethod
    def lessons_block(lessons):
        if not lessons:
            return ""
        lines = "\n".join(f"- {l['text']}" for l in lessons)
        return f"Lessons learned from previous tasks (follow them unless clearly wrong):\n{lines}"

    # -- learning after a task ---------------------------------------------------
    def add_lesson(self, text, scope="global", tags=(), source_task=None, score=1.0, pending=False):
        text = (text or "").strip()
        if not text:
            return None
        dup = self.db.one("SELECT id FROM lessons WHERE text=? AND scope=?", (text, scope))
        if dup:
            self.db.execute("UPDATE lessons SET score=score+0.5 WHERE id=?", (dup["id"],))
            return dup["id"]
        return self.db.execute(
            "INSERT INTO lessons (scope, text, tags, source_task, score, created_at, pending) "
            "VALUES (?,?,?,?,?,?,?)",
            (scope, text, json.dumps(list(tags)), source_task, score, time.time(), 1 if pending else 0))

    def record_permission_denials(self, task_id, denials):
        for d in denials or []:
            rule = suggest_rule(d.get("tool_name") or "?", d.get("tool_input") or {})
            if not rule:
                continue
            self.db.execute(
                "INSERT INTO permission_suggestions (rule, last_task) VALUES (?,?) "
                "ON CONFLICT(rule) DO UPDATE SET count=count+1, last_task=excluded.last_task",
                (rule, task_id))

    def reflect(self, task):
        """Extract lessons from a finished task. Uses the local 'reflect' model when available,
        otherwise falls back to rule-based lessons."""
        if not self.cfg["learning"]["enabled"]:
            return []
        added = []
        workdir = task.get("workdir") or ""
        verify = task.get("verify") or {}
        if verify.get("ok") is False and task.get("verify_cmd"):
            added.append(self.add_lesson(
                f"Before finishing, run `{task['verify_cmd']}` yourself and make it pass.",
                scope=workdir or "global", tags=["verify"], source_task=task["id"]))
        if task.get("feedback") and task.get("rating") == -1:
            added.append(self.add_lesson(
                f"User rejected a previous '{task.get('category')}' task with feedback: "
                f"{task['feedback'][:300]}", scope=workdir or "global",
                tags=[task.get("category") or "feedback"], source_task=task["id"], score=2.0))
        if self.cfg["learning"].get("llm_reflection") and self.local.enabled:
            summary = {
                "prompt": (task.get("prompt") or "")[:3000],
                "status": task.get("status"), "agent": task.get("agent"),
                "report": task.get("report"), "verify": verify,
                "error": task.get("error"), "user_feedback": task.get("feedback"),
                "tool_errors": [e["data"].get("text", "")[:300] for e in
                                self.db.events(task["id"]) if e["kind"] == "tool_error"][:8],
            }
            out = self.local.chat_json("reflect", REFLECT_SYSTEM,
                                       json.dumps(summary, ensure_ascii=False, default=str))
            for l in (out or {}).get("lessons", [])[:4]:
                if isinstance(l, dict) and l.get("text"):
                    scope = workdir if l.get("scope") == "project" and workdir else "global"
                    added.append(self.add_lesson(
                        l["text"], scope, l.get("tags") or [], task["id"],
                        pending=not self.cfg["learning"].get("auto_approve_llm_lessons")))
        return [a for a in added if a]

    def feedback(self, task, accepted):
        """User accepted/rejected: re-weight the lessons that were used for this task."""
        delta = 0.5 if accepted else -0.5
        used = {i for e in self.db.events(task["id"]) if e["kind"] == "lessons" for i in e["data"].get("ids", [])}
        for lid in used:
            self.db.execute("UPDATE lessons SET score=score+? WHERE id=?", (delta, lid))
        self.db.execute("UPDATE usage SET ok=? WHERE task_id=? AND id=(SELECT MAX(id) FROM usage "
                        "WHERE task_id=?)", (1 if accepted else 0, task["id"], task["id"]))
