"""Scheduler + workers. Picks queued tasks, routes them, runs them, verifies, reports, learns."""
import subprocess
import threading
import time
import traceback
import urllib.request
from pathlib import Path

from .accounts import AccountPool
from .agents.claude import ClaudeRun, handoff_session
from .agents.local import LocalLLM
from .agents.report import REPORT_INSTRUCTIONS, parse_report, strip_report
from .learning import Learner
from .router import Router

CONTINUE_MSG = ("You were interrupted (the orchestrator switched Claude accounts). "
                "Continue the task exactly where you left off, then finish with the report block.")


class Orchestrator:
    def __init__(self, cfg, db, bus):
        self.cfg, self.db, self.bus = cfg, db, bus
        self.local = LocalLLM(cfg)
        self.pool = AccountPool(cfg, db)
        self.router = Router(cfg, db, self.local)
        self.learner = Learner(cfg, db, self.local)
        self.active = {}          # task_id -> ClaudeRun (for cancel)
        self.busy_dirs = set()    # one writer per working directory
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.stall_flagged = set()
        self.routing = set()      # task ids being classified off the scheduler thread
        # Tasks left "running" by a previous crash go back to the queue.
        db.execute("UPDATE tasks SET status='queued' WHERE status='running'")

    # -- events ------------------------------------------------------------------
    def emit(self, task_id, kind, data):
        ev = self.db.add_event(task_id, kind, data)
        self.db.update_task(task_id, last_event_at=ev["ts"])
        self.bus.publish({"type": "event", "event": ev})

    def changed(self, task_id):
        self.bus.publish({"type": "task", "task": self.db.task(task_id)})

    def notify(self, title, body):
        url = self.cfg["notify"].get("ntfy_url")
        if not url:
            return
        try:
            req = urllib.request.Request(url, body.encode(), {"Title": title.encode("utf-8")})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            pass

    # -- public API ----------------------------------------------------------------
    def submit(self, prompt, title="", workdir="", agent_pref="auto", verify_cmd=""):
        tid = self.db.create_task(prompt=prompt, title=title or prompt.strip().split("\n")[0][:80],
                                  workdir=workdir, agent_pref=agent_pref, verify_cmd=verify_cmd,
                                  status="queued")
        self.emit(tid, "created", {"agent_pref": agent_pref})
        self.changed(tid)
        return tid

    def reply(self, task_id, message):
        """Answer a needs_input task, or send follow-up instructions to a finished one."""
        self.db.update_task(task_id, pending_message=message, status="queued", error=None)
        self.emit(task_id, "user_message", {"text": message})
        self.changed(task_id)

    def cancel(self, task_id):
        run = self.active.get(task_id)
        if run:
            run.cancel()
        self.db.update_task(task_id, status="cancelled", finished_at=time.time())
        self.emit(task_id, "cancelled", {})
        self.changed(task_id)

    def review(self, task_id, accepted, feedback=""):
        task = self.db.task(task_id)
        self.db.update_task(task_id, rating=1 if accepted else -1, feedback=feedback or None,
                            status="done" if accepted else task["status"])
        self.learner.feedback(task, accepted)
        self.emit(task_id, "review", {"accepted": accepted, "feedback": feedback})
        task = self.db.task(task_id)
        if not accepted:
            self.learner.reflect(task)
            if feedback:
                self.reply(task_id, f"The user reviewed your work and rejected it:\n{feedback}\n"
                                    "Fix it, then finish with the report block.")
        self.changed(task_id)

    # -- scheduler -------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="scheduler").start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self._tick()
            except Exception:
                traceback.print_exc()
            self.stop.wait(1.0)

    def _tick(self):
        now = time.time()
        stall = self.cfg["claude"]["stall_after_sec"]
        for t in self.db.query("SELECT id, last_event_at FROM tasks WHERE status='running'"):
            if t["last_event_at"] and now - t["last_event_at"] > stall and t["id"] not in self.stall_flagged:
                self.stall_flagged.add(t["id"])
                self.emit(t["id"], "stalled", {"idle_sec": int(now - t["last_event_at"])})
                self.notify(f"Task #{t['id']} stalled", f"No activity for {int(now - t['last_event_at'])}s")
        for task in self.db.query("SELECT * FROM tasks WHERE status='queued' ORDER BY id"):
            self._try_start(task)

    def _route(self, task):
        """Classification may call a local LLM (seconds), so it runs off the scheduler thread."""
        try:
            agent, cat, cx, reason = self.router.route(task)
            self.db.update_task(task["id"], agent=agent, category=cat, complexity=cx, route_reason=reason)
            self.emit(task["id"], "routed", {"agent": agent, "category": cat, "complexity": cx,
                                             "reason": reason})
        finally:
            self.routing.discard(task["id"])

    def _try_start(self, task):
        if task["id"] in self.routing:
            return
        if not task.get("agent"):
            self.routing.add(task["id"])
            threading.Thread(target=self._route, args=(task,), daemon=True).start()
            return
        if not task.get("workdir"):
            # Never let an agent loose in the orchestrator's own directory.
            wd = Path(self.cfg["data_dir"]) / "workspaces" / f"task-{task['id']}"
            wd.mkdir(parents=True, exist_ok=True)
            self.db.update_task(task["id"], workdir=str(wd))
            task = self.db.task(task["id"])
        wd = task.get("workdir") or ""
        with self.lock:
            if wd and wd in self.busy_dirs:
                return
        account = None
        if task["agent"] == "claude":
            account = self.pool.acquire()
            if not account:
                return  # all accounts busy or cooling down; stay queued
        with self.lock:
            if wd:
                self.busy_dirs.add(wd)
        self.db.update_task(task["id"], status="running", started_at=task.get("started_at") or time.time(),
                            attempts=(task.get("attempts") or 0) + 1, last_event_at=time.time())
        self.stall_flagged.discard(task["id"])
        self.changed(task["id"])
        target = self._run_claude if account else self._run_local
        args = (task, account) if account else (task,)
        threading.Thread(target=self._guard, args=(target, task, account, args), daemon=True).start()

    def _guard(self, target, task, account, args):
        try:
            target(*args)
        except Exception as e:
            traceback.print_exc()
            self.db.update_task(task["id"], status="failed", error=f"orchestrator error: {e}")
            self.emit(task["id"], "error", {"text": str(e)})
        finally:
            if account:
                self.pool.release(account["name"])
            with self.lock:
                self.busy_dirs.discard(task.get("workdir") or "")
            self.active.pop(task["id"], None)
            self.changed(task["id"])

    # -- Claude --------------------------------------------------------------------------
    def _system_prompt(self, task):
        lessons = self.learner.relevant_lessons(task["prompt"], task.get("workdir"))
        if lessons:
            self.emit(task["id"], "lessons", {"ids": [l["id"] for l in lessons],
                                              "texts": [l["text"] for l in lessons]})
        return "\n\n".join(x for x in (REPORT_INSTRUCTIONS, self.learner.lessons_block(lessons)) if x)

    def _run_claude(self, task, account):
        tid = task["id"]
        resume, prompt = None, task["prompt"]
        if task.get("session_id") and task.get("pending_message"):
            prompt = task["pending_message"]
            resume = task["session_id"]
            prev = next((a for a in self.cfg["accounts"] if a["name"] == task.get("account")), None)
            if prev and prev["name"] != account["name"]:
                if handoff_session(resume, prev, account):
                    self.emit(tid, "handoff", {"from": prev["name"], "to": account["name"]})
                else:
                    self.emit(tid, "handoff_failed", {"from": prev["name"], "to": account["name"]})
                    resume, prompt = None, f"{task['prompt']}\n\n(Earlier attempt was interrupted; " \
                                           f"check the working tree for partial progress.)\n{task['pending_message']}"
        self.db.update_task(tid, account=account["name"], pending_message=None)
        self.emit(tid, "start", {"agent": "claude", "account": account["name"], "resume": bool(resume)})

        def on_event(kind, data):
            if kind == "init":
                self.db.update_task(tid, session_id=data["session_id"], model=data["model"])
            if kind == "rate_limit":
                self.pool.record_limits(account["name"], data)
                self.bus.publish({"type": "stats"})
                return  # stored per account; too noisy for the task log
            if kind == "todos":
                todos = data["todos"]
                done = sum(1 for t in todos if t["status"] == "completed")
                self.db.update_task(tid, todos=todos, progress=done / len(todos) if todos else 0)
                self.changed(tid)
            self.emit(tid, kind, data)

        run = ClaudeRun(self.cfg, account, prompt, task.get("workdir"), self._system_prompt(task),
                        on_event, resume_session=resume)
        self.active[tid] = run
        res = run.run()
        if run.cancelled:
            return
        cur = self.db.task(tid)
        self.db.update_task(
            tid, cost_usd=(cur["cost_usd"] or 0) + res["cost_usd"],
            input_tokens=(cur["input_tokens"] or 0) + res["input_tokens"],
            output_tokens=(cur["output_tokens"] or 0) + res["output_tokens"],
            duration_ms=(cur["duration_ms"] or 0) + res["duration_ms"],
            num_turns=(cur["num_turns"] or 0) + res["num_turns"],
            session_id=res["session_id"], model=res["model"])
        self.db.add_usage(task_id=tid, kind="claude", name=account["name"], model=res["model"],
                          cost_usd=res["cost_usd"], input_tokens=res["input_tokens"],
                          output_tokens=res["output_tokens"], duration_ms=res["duration_ms"],
                          ok=1 if res["ok"] else 0, category=task.get("category"))
        self.learner.record_permission_denials(tid, res["permission_denials"])
        if res["permission_denials"]:
            self.emit(tid, "permission_denials", {"items": res["permission_denials"][:20]})

        if res["limit"]:
            until = self.pool.cool_down(account["name"], res["reset_at"], res["error"] or "")
            self.emit(tid, "account_limit", {"account": account["name"], "until": until})
            self.notify("Account limit", f"{account['name']} hit its limit; switching account")
            if self.cfg["claude"]["handoff_on_limit"] and res["session_id"]:
                self.db.update_task(tid, status="queued", pending_message=CONTINUE_MSG)
            else:
                self.db.update_task(tid, status="queued")
            return
        self._finish(self.db.task(tid), res["ok"], res["text"], res["error"])

    # -- local ---------------------------------------------------------------------------
    def _run_local(self, task):
        tid = task["id"]
        model = self.local.model_for("simple")
        prompt = task["prompt"]
        if task.get("pending_message"):
            prompt += f"\n\nPrevious answer:\n{task.get('result_text') or ''}\n\n" \
                      f"Follow-up from the user:\n{task['pending_message']}"
        self.db.update_task(tid, model=model["name"], account=None, pending_message=None)
        self.emit(tid, "start", {"agent": "local", "model": model["name"]})
        system = ("You are a precise assistant. Answer the task directly and completely.\n\n"
                  + self._system_prompt(task))
        try:
            out = self.local.chat(model, [{"role": "system", "content": system},
                                          {"role": "user", "content": prompt}])
            ok, text, err = bool(out["text"].strip()), out["text"], None if out["text"].strip() else "empty answer"
        except Exception as e:
            out, ok, text, err = {"input_tokens": 0, "output_tokens": 0, "duration_ms": 0}, False, "", str(e)
        self.emit(tid, "text", {"text": text[:4000]} if ok else {"text": f"local error: {err}"})
        cur = self.db.task(tid)
        self.db.update_task(tid, input_tokens=(cur["input_tokens"] or 0) + out["input_tokens"],
                            output_tokens=(cur["output_tokens"] or 0) + out["output_tokens"],
                            duration_ms=(cur["duration_ms"] or 0) + out["duration_ms"])
        self.db.add_usage(task_id=tid, kind="local", name=model["name"], model=model["name"], cost_usd=0,
                          input_tokens=out["input_tokens"], output_tokens=out["output_tokens"],
                          duration_ms=out["duration_ms"], ok=None if ok else 0,  # review decides
                          category=task.get("category"))
        if not ok and self.cfg["local"]["escalate_to_claude"] and self.pool.accounts:
            self.db.update_task(tid, status="queued", agent="claude", agent_pref="claude",
                                route_reason=f"escalated from local: {err}")
            self.emit(tid, "escalated", {"to": "claude", "reason": err})
            return
        self._finish(self.db.task(tid), ok, text, err)

    # -- completion ----------------------------------------------------------------------
    def _finish(self, task, ok, text, error):
        tid = task["id"]
        report = parse_report(text) or ({"status": "done" if ok else "failed",
                                         "summary": strip_report(text)[:1500],
                                         "output": [], "test_cases": []} if text else None)
        changed_files = git_changes(task.get("workdir"))
        verify = run_verify(task) if ok and task.get("verify_cmd") else None
        if verify:
            self.emit(tid, "verify", verify)
            if not verify["ok"]:
                self.db.execute("UPDATE usage SET ok=0 WHERE id=(SELECT MAX(id) FROM usage WHERE task_id=?)",
                                (tid,))
        if not ok:
            status = "failed"
        elif report and report.get("status") == "needs_input":
            status = "needs_input"
        elif verify and not verify["ok"]:
            status = "review"
        else:
            status = "done" if self.cfg.get("auto_accept") else "review"
        progress = 1.0 if status in ("done", "review") else task.get("progress") or 0
        self.db.update_task(tid, status=status, report=report, result_text=strip_report(text),
                            error=error if not ok else None, verify=verify, changed_files=changed_files,
                            progress=progress, finished_at=time.time())
        self.emit(tid, "finished", {"status": status})
        task = self.db.task(tid)
        lessons = self.learner.reflect(task)
        if lessons:
            self.emit(tid, "learned", {"lesson_ids": lessons})
        label = {"review": "ready for review", "needs_input": "needs your input"}.get(status, status)
        self.notify(f"Task #{tid} {label}", (report or {}).get("summary", error or "")[:300])


def git_changes(workdir):
    if not workdir:
        return None
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=workdir, capture_output=True,
                             text=True, timeout=20)
        if out.returncode:
            return None
        return [line[3:] for line in out.stdout.splitlines() if line.strip()][:200]
    except Exception:
        return None


def run_verify(task):
    t0 = time.time()
    try:
        p = subprocess.run(task["verify_cmd"], shell=True, cwd=task.get("workdir") or None,
                           capture_output=True, text=True, timeout=900)
        out = (p.stdout + p.stderr)[-4000:]
        return {"cmd": task["verify_cmd"], "ok": p.returncode == 0, "code": p.returncode,
                "output": out, "sec": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired:
        return {"cmd": task["verify_cmd"], "ok": False, "code": None, "output": "timeout", "sec": 900}
