"""The "brain": decides which agent gets a task, and learns from outcomes via success stats."""
import re

# Keyword heuristics (English + Vietnamese). Used when no local classifier is available.
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

CLASSIFIER_SYSTEM = """Classify a software task. Reply with JSON only:
{"category": one of [summarize, translate, explain, commit_message, docs, classify, regex, snippet,
chat, bugfix, refactor, test, feature, devops, research, other],
 "complexity": 1-5 (1 = one-shot text answer, 5 = large multi-file engineering),
 "needs_repo": true if it must read or modify files / run commands}"""


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
    def __init__(self, cfg, db, local):
        self.cfg = cfg
        self.db = db
        self.local = local

    def classify(self, prompt):
        if self.cfg["router"].get("use_llm_classifier") and self.local.enabled:
            out = self.local.chat_json("router", CLASSIFIER_SYSTEM, prompt[:4000])
            if isinstance(out, dict) and out.get("category"):
                try:
                    out["complexity"] = int(out.get("complexity", 3))
                except (TypeError, ValueError):
                    out["complexity"] = 3
                out["source"] = "llm"
                return out
        out = heuristic_classify(prompt)
        out["source"] = "heuristic"
        return out

    def success_rate(self, kind, category):
        row = self.db.one("SELECT COUNT(ok) n, AVG(ok) rate FROM usage WHERE kind=? AND category=?",
                          (kind, category))
        return (row["n"] or 0), (row["rate"] if row["rate"] is not None else None)

    def route(self, task):
        """Returns (agent_kind, category, complexity, reason)."""
        pref = task.get("agent_pref") or "auto"
        cls = self.classify(task["prompt"])
        cat, cx = cls.get("category", "other"), cls.get("complexity", 3)
        if pref in ("claude", "local"):
            return pref, cat, cx, f"user chose {pref}"
        r = self.cfg["router"]
        if not self.local.enabled or not self.local.model_for("simple"):
            return "claude", cat, cx, "no local model configured"
        if cls.get("needs_repo"):
            return "claude", cat, cx, f"{cat}: needs to work inside the repo"
        if cat not in r["local_categories"]:
            return "claude", cat, cx, f"{cat} is not a local category"
        if cx > r["local_max_complexity"]:
            return "claude", cat, cx, f"complexity {cx} > local max {r['local_max_complexity']}"
        n, rate = self.success_rate("local", cat)
        if n >= r["min_samples"] and rate is not None and rate < r["min_local_success"]:
            return "claude", cat, cx, f"local success on {cat} is {rate:.0%} over {n} tasks (learned)"
        return "local", cat, cx, f"simple {cat} (complexity {cx}) -> local ({cls['source']} classifier)"
