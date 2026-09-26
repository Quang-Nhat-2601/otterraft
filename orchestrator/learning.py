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
            "SELECT * FROM lessons WHERE enabled=1 AND (scope='global' OR scope=?)", (workdir or "",))
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
    def add_lesson(self, text, scope="global", tags=(), source_task=None, score=1.0):
        text = (text or "").strip()
        if not text:
            return None
        dup = self.db.one("SELECT id FROM lessons WHERE text=? AND scope=?", (text, scope))
        if dup:
            self.db.execute("UPDATE lessons SET score=score+0.5 WHERE id=?", (dup["id"],))
            return dup["id"]
        return self.db.execute(
            "INSERT INTO lessons (scope, text, tags, source_task, score, created_at) VALUES (?,?,?,?,?,?)",
            (scope, text, json.dumps(list(tags)), source_task, score, time.time()))

    def record_permission_denials(self, task_id, denials):
        for d in denials or []:
            tool = d.get("tool_name") or "?"
            inp = d.get("tool_input") or {}
            rule = tool
            if tool == "Bash" and inp.get("command"):
                rule = f"Bash({inp['command'].split()[0]} *)"
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
                    added.append(self.add_lesson(l["text"], scope, l.get("tags") or [], task["id"]))
        return [a for a in added if a]

    def feedback(self, task, accepted):
        """User accepted/rejected: re-weight the lessons that were used for this task."""
        delta = 0.5 if accepted else -0.5
        self.db.execute("UPDATE lessons SET score=score+? WHERE source_task=?", (delta, task["id"]))
        self.db.execute("UPDATE usage SET ok=? WHERE task_id=? AND id=(SELECT MAX(id) FROM usage "
                        "WHERE task_id=?)", (1 if accepted else 0, task["id"], task["id"]))
