import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import config  # noqa: E402
from orchestrator.accounts import detect_limit  # noqa: E402
from orchestrator.agents.report import parse_report  # noqa: E402
from orchestrator.bus import Bus  # noqa: E402
from orchestrator.db import DB  # noqa: E402
from orchestrator.router import heuristic_classify  # noqa: E402
from orchestrator.runner import Orchestrator  # noqa: E402
from orchestrator.server import serve  # noqa: E402

FAKE_CLAUDE = str(Path(__file__).with_name("fake_claude.py"))


class FakeOllama(BaseHTTPRequestHandler):
    fail_on = "FAILME"

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json({"models": [{"name": "tiny"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system, user = req["messages"][0]["content"], req["messages"][-1]["content"]
        if req.get("format") == "json" and "Classify" in system:
            cat = "summarize" if "tóm tắt" in user.lower() else "bugfix"
            content = json.dumps({"category": cat, "complexity": 1 if cat == "summarize" else 4,
                                  "needs_repo": cat != "summarize"})
        elif req.get("format") == "json":
            content = json.dumps({"lessons": [{"text": "Run pytest -q before finishing", "scope": "project"}]})
        elif self.fail_on in user:
            content = ""
        else:
            content = "Tóm tắt: nội dung ngắn gọn."
        self._json({"message": {"content": content}, "prompt_eval_count": 50, "eval_count": 20})


def wait_for(fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("timed out")


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.ollama = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.ollama.serve_forever, daemon=True).start()
        cfg_path = Path(cls.tmp) / "orchestrator.json"
        cfg_path.write_text(json.dumps({
            "port": 0, "data_dir": cls.tmp + "/data", "claude_bin": FAKE_CLAUDE,
            "accounts": [
                {"name": "acc1", "config_dir": cls.tmp + "/limited-acc1", "priority": 1},
                {"name": "acc2", "config_dir": cls.tmp + "/acc2", "priority": 2},
            ],
            "local": {"ollama_url": f"http://127.0.0.1:{cls.ollama.server_port}",
                      "models": [{"name": "tiny", "roles": ["simple", "router", "reflect"]}]},
        }))
        cls.cfg = config.load(cfg_path)
        cls.orch = Orchestrator(cls.cfg, DB(Path(cls.tmp) / "data" / "o.db"), Bus())
        cls.orch.start()
        cls.httpd = serve(cls.orch)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.workdir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.orch.stop.set()
        cls.httpd.shutdown()
        cls.ollama.shutdown()

    def api(self, path, body=None):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_claude_task_switches_account_and_hands_off(self):
        tid = self.api("/api/tasks", {"prompt": "Fix the login bug in app.py and add a test",
                                      "workdir": self.workdir, "verify_cmd": "true"})["id"]
        t = wait_for(lambda: (lambda t: t if t["status"] in ("review", "failed") else None)(
            self.api(f"/api/tasks/{tid}")))
        self.assertEqual(t["status"], "review", t.get("error"))
        self.assertEqual(t["agent"], "claude")
        self.assertEqual(t["account"], "acc2")
        self.assertIn("after resume", t["report"]["summary"])
        self.assertEqual(t["report"]["test_cases"][0]["title"], "Login with blank email")
        self.assertTrue(t["verify"]["ok"])
        self.assertEqual(t["progress"], 1.0)
        kinds = [e["kind"] for e in self.api(f"/api/tasks/{tid}/events")]
        for k in ("routed", "account_limit", "handoff", "todos", "tool", "verify", "finished", "learned"):
            self.assertIn(k, kinds)
        acc = {a["name"]: a for a in self.api("/api/stats")["accounts"]}
        self.assertGreater(acc["acc1"]["cooldown_until"], time.time())
        self.assertEqual(acc["acc2"]["all"]["n"], 1)
        self.assertEqual(acc["acc2"]["utilization_5h"], 0.3)
        self.assertEqual(acc["acc1"]["utilization_5h"], 0.97)
        perms = self.api("/api/permissions")
        self.assertEqual(perms[0]["rule"], "Bash(rm *)")
        lessons = self.api("/api/lessons")
        self.assertTrue(any("pytest" in l["text"] for l in lessons))

        # user rejects -> task resumes with feedback, learns a lesson
        self.api(f"/api/tasks/{tid}/review", {"accepted": False, "feedback": "Also handle whitespace"})
        wait_for(lambda: self.api(f"/api/tasks/{tid}")["status"] == "review")
        self.assertTrue(any("whitespace" in l["text"] for l in self.api("/api/lessons")))
        self.api(f"/api/tasks/{tid}/review", {"accepted": True})
        self.assertEqual(self.api(f"/api/tasks/{tid}")["status"], "done")

    def test_simple_task_goes_local(self):
        tid = self.api("/api/tasks", {"prompt": "Tóm tắt đoạn văn này: AI giúp lập trình viên."})["id"]
        t = wait_for(lambda: (lambda t: t if t["status"] in ("review", "failed") else None)(
            self.api(f"/api/tasks/{tid}")))
        self.assertEqual(t["agent"], "local")
        self.assertIn("Tóm tắt", t["result_text"])
        self.assertEqual(t["status"], "review")

    def test_local_failure_escalates_to_claude(self):
        self.orch.pool.reset("acc1")
        self.orch.pool.cool_down("acc1", time.time() + 3600)
        tid = self.api("/api/tasks", {"prompt": "Tóm tắt FAILME"})["id"]
        t = wait_for(lambda: (lambda t: t if t["status"] in ("review", "failed") else None)(
            self.api(f"/api/tasks/{tid}")))
        self.assertEqual(t["agent"], "claude")
        self.assertIn("escalated", t["route_reason"])

    def test_dashboard_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"AI Orchestrator", r.read())


class Units(unittest.TestCase):
    def test_detect_limit(self):
        self.assertEqual(detect_limit("Claude AI usage limit reached|1737000000"), (True, 1737000000))
        self.assertEqual(detect_limit("You've hit your limit · resets 3pm"), (True, None))
        self.assertEqual(detect_limit("SyntaxError in foo.py"), (False, None))

    def test_parse_report(self):
        r = parse_report('blah\n```orchestrator-report\n{"status":"done","summary":"x"}\n```')
        self.assertEqual(r["summary"], "x")
        self.assertEqual(r["test_cases"], [])
        self.assertIsNone(parse_report("no report here"))

    def test_prefers_account_under_threshold(self):
        from orchestrator.accounts import AccountPool
        tmp = tempfile.mkdtemp()
        cfg = {"accounts": [{"name": "a", "priority": 1}, {"name": "b", "priority": 2}],
               "claude": {"switch_at_utilization": 0.9, "default_cooldown_min": 60}}
        pool = AccountPool(cfg, DB(Path(tmp) / "x.db"))
        self.assertEqual(pool.acquire()["name"], "a")
        pool.release("a")
        pool.record_limits("a", {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.95, "resetsAt": time.time() + 3600}}})
        self.assertEqual(pool.acquire()["name"], "b")
        pool.record_limits("b", {"status": "rejected", "resetsAt": time.time() + 3600})
        pool.release("b")
        self.assertEqual([a["name"] for a in pool.available()], ["a"])

    def test_heuristic_classify(self):
        self.assertEqual(heuristic_classify("Dịch đoạn này sang tiếng Anh")["category"], "translate")
        c = heuristic_classify("Sửa lỗi crash trong src/app.py")
        self.assertEqual(c["category"], "bugfix")
        self.assertTrue(c["needs_repo"])


if __name__ == "__main__":
    unittest.main()
