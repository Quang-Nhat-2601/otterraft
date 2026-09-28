"""The "brain": decides which agent gets a task, and learns from outcomes via success stats."""
import os
import re

# Keyword rules, the last fallback when neither Claude nor a local model can classify a task.
# They match English and Vietnamese on purpose: users write tasks in their own language.
CATEGORY_RULES = [
    ("commit_message", r"commit message|viết commit|message commit"),
    ("translate", r"translate|dịch( sang| qua)?\b"),
    ("summarize", r"summari[sz]e|tóm tắt|tldr|tl;dr"),
    ("regex", r"\bregex\b|regular expression|biểu thức chính quy"),
    ("explain", r"^(explain|what is|why|giải thích|là gì|tại sao)|giải thích"),
    ("docs", r"docstring|readme|document(ation)?|viết tài liệu"),
    ("classify", r"classify|phân loại|label"),
    ("bugfix", r"\bbug\b|fix|lỗi|sửa|crash|exception|traceback|failing"),
    ("refactor", r"refactor|restructure|tái cấu trúc|clean ?up"),
    ("test", r"\btests?\b|unit test|kiểm thử|coverage"),
    ("feature", r"implement|build|add (a |an )?feature|tạo|xây dựng|thêm tính năng|làm (một|cái)"),
    ("devops", r"deploy|docker|ci\b|pipeline|kubernetes|terraform"),
]
REPO_HINTS = r"repo|codebase|file|folder|thư mục|project|dự án|module|\.py\b|\.ts\b|\.js\b|src/"

CLASSIFIER_SYSTEM = """You are the dispatcher of OtterRaft, an AI task orchestrator. Classify the task you are given.
Reply with one JSON object and nothing else:
{"category": one of [summarize, translate, explain, commit_message, docs, classify, regex, snippet,
chat, bugfix, refactor, test, feature, devops, research, other],
 "complexity": 1-5 (1 = one-shot text answer, 3 = a focused change in a few files, 5 = large multi-file engineering),
 "needs_repo": true if doing it requires reading or changing files or running commands, false if a text answer is enough}"""

def heuristic_classify(prompt):
    p = prompt.lower()
    category = "other"
    for cat, rx in CATEGORY_RULES:
        if re.search(rx, p, re.M):
            category = cat
            break
    needs_repo = bool(re.search(REPO_HINTS, p)) or category in ("bugfix", "refactor", "test",
                                                                 "feature", "devops")
    words = len(p.split())
    complexity = 1 if words < 40 else 2 if words < 150 else 3
    if needs_repo:
        complexity = max(complexity, 3)
    return {"category": category, "complexity": complexity, "needs_repo": needs_repo}


class Router:
    """Picks who does a task:
    - claude: a full Claude Code agent (tools, repo, worktree)
    - quick:  one lean Claude call, no tools; for text answers (summaries, translations, messages)
    - local:  a local model, when you enabled one
    The classification itself comes from the "brain": a lean Claude call (Sonnet by default),
    falling back to a local model, then to keyword rules."""

    def __init__(self, cfg, db, local, pool=None):
        self.cfg = cfg
        self.db = db
        self.local = local
        self.pool = pool

    def classify(self, prompt, task_id=None):
        provider = self.cfg["brain"].get("provider", "claude")
        if provider == "claude" and self.pool:
            out = self._claude_classify(prompt, task_id)
            if out:
                return out
        if provider in ("claude", "local") and self.local.enabled and self.local.model_for("router") \
                and self.cfg["router"].get("use_llm_classifier", True):
            out = self.local.chat_json("router", CLASSIFIER_SYSTEM, prompt[:4000])
            if isinstance(out, dict) and out.get("category"):
                return self._clean(out, "local")
        out = heuristic_classify(prompt)
        out["source"] = "keywords"
        return out

    def _claude_classify(self, prompt, task_id):
        from .agents.claude import parse_json_answer, quick_call
        # A dedicated variable: a plain ANTHROPIC_API_KEY would also switch the user's own sessions to API billing.
        key = os.environ.get("OTTERRAFT_BRAIN_API_KEY")
        account = {"name": "brain-api", "use_api_key": True, "env": {"ANTHROPIC_API_KEY": key}} \
            if key else self.pool.best_available()
        if not account:
            return None
        b = self.cfg["brain"]
        res = quick_call(self.cfg, account, CLASSIFIER_SYSTEM, prompt[:12000], b.get("model", "sonnet"),
                         timeout=b.get("timeout_sec", 60))
        self.db.add_usage(task_id=task_id, kind="brain", name=account["name"], model=res["model"],
                          cost_usd=res["cost_usd"], input_tokens=res["input_tokens"],
                          output_tokens=res["output_tokens"], cached_tokens=res["cached_tokens"],
                          duration_ms=res["duration_ms"], ok=1 if res["ok"] else 0, category="routing")
        if not res["ok"]:
            if key:
                pass  # the API key isn't a pool account: nothing to cool down or park
            elif res["failure"] == "quota":
                self.pool.cool_down(account["name"], res["reset_at"] or self.pool.window_reset(account["name"]),
                                    res["error"] or "")
            elif res["failure"] == "login":
                self.pool.park_login(account["name"])
            return None
        out = parse_json_answer(res["text"])
        return self._clean(out, f"claude {res['model']}") if isinstance(out, dict) and out.get("category") else None

    @staticmethod
    def _clean(out, source):
        try:
            out["complexity"] = min(5, max(1, int(out.get("complexity", 3))))
        except (TypeError, ValueError):
            out["complexity"] = 3
        out["needs_repo"] = bool(out.get("needs_repo"))
        out["source"] = source
        return out

    def success_rate(self, kind, category):
        row = self.db.one("SELECT COUNT(ok) n, AVG(ok) rate FROM usage WHERE kind=? AND category=?",
                          (kind, category))
        return (row["n"] or 0), (row["rate"] if row["rate"] is not None else None)

    def _proven_bad(self, kind, cat):
        r = self.cfg["router"]
        n, rate = self.success_rate(kind, cat)
        if n >= r["min_samples"] and rate is not None and rate < r["min_local_success"]:
            return f"{kind} success on {cat} is {rate:.0%} over {n} tasks (learned)"
        return None

    def route(self, task):
        """Returns (agent_kind, category, complexity, reason)."""
        pref = task.get("agent_pref") or "auto"
        cls = self.classify(task["prompt"], task.get("id"))
        cat, cx, src = cls.get("category", "other"), cls.get("complexity", 3), cls["source"]
        if pref in ("claude", "local", "quick"):
            return pref, cat, cx, f"user chose {pref}"
        r = self.cfg["router"]
        if cls.get("needs_repo"):
            return "claude", cat, cx, f"{cat}: needs to work inside the repo ({src})"
        if cat not in r["local_categories"]:
            return "claude", cat, cx, f"{cat} is not a text-only category ({src})"
        why_not = []
        if self.local.enabled and self.local.model_for("simple"):
            bad = self._proven_bad("local", cat)
            if cx <= r["local_max_complexity"] and not bad:
                return "local", cat, cx, f"text task {cat} (complexity {cx}) -> local model ({src})"
            why_not.append(bad or f"complexity {cx} > local max {r['local_max_complexity']}")
        q = self.cfg["quick"]
        if q.get("enabled"):
            bad = self._proven_bad("quick", cat)
            if cx <= q.get("max_complexity", 3) and not bad:
                return "quick", cat, cx, f"text task {cat} (complexity {cx}) -> quick Claude answer ({src})" + \
                    (f"; not local: {why_not[0]}" if why_not else "")
            why_not.append(bad or f"complexity {cx} > quick max {q.get('max_complexity', 3)}")
        return "claude", cat, cx, f"{cat} -> Claude agent ({src})" + (f"; {'; '.join(why_not)}" if why_not else "")
