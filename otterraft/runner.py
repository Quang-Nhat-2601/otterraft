"""Scheduler + workers. Picks queued tasks, routes them, runs them, verifies, reports, learns."""
import subprocess
import threading
import time
import traceback
import urllib.request
from pathlib import Path

from . import workspace
from .accounts import AccountPool
from .agents.claude import ClaudeRun, handoff_session, quick_call
from .agents.local import LocalLLM
from .agents.report import REPORT_INSTRUCTIONS, parse_report, strip_report
from .coach import COACH_SYSTEM, Coach
from .learning import Learner
from .router import Router

CONTINUE_MSG = ("You were interrupted (provider limit, account switch or connection problem). "
                "Continue the task exactly where you left off, then finish with the report block.")
RESTART_NOTE = ("\n\n(An earlier attempt at this task was interrupted and could not be resumed. "
                "Check the working tree for partial progress before redoing anything.)")
QUICK_SYSTEM = ("You answer tasks from a software developer's task queue. Answer directly and "
                "completely, in the language the task is written in. You have no tools and cannot "
                "read files or run commands; if the task needs something you were not given, say "
                "exactly what is missing instead of guessing.")


class Orchestrator:
    def __init__(self, cfg, db, bus):
        self.cfg, self.db, self.bus = cfg, db, bus
        self.local = LocalLLM(cfg)
        self.pool = AccountPool(cfg, db)
        self.router = Router(cfg, db, self.local, self.pool)
        self.learner = Learner(cfg, db, self.local)
        self.coach = Coach(cfg, db)
        self.active = {}          # task_id -> ClaudeRun (for cancel)
        self.busy = {}            # task_id -> lock key (a directory, or the task itself)
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
    def submit(self, prompt, title="", workdir="", agent_pref="auto", verify_cmd="", kind="task", **extra):
        tid = self.db.create_task(prompt=prompt, title=title or prompt.strip().split("\n")[0][:80],
                                  workdir=workdir, agent_pref=agent_pref, verify_cmd=verify_cmd,
                                  status="queued", kind=kind, **extra)
        self.emit(tid, "created", {"agent_pref": agent_pref, "kind": kind})
        self.changed(tid)
        return tid

    def run_coach(self, manual=False):
        """Queue a Reflection Coach run over tasks finished since the last one. A manual run with
        nothing new looks back 30 days instead. Returns the task id, or None if there is no evidence."""
        run_dir, n = self.coach.prepare_run()
        if not run_dir and manual:
            run_dir, n = self.coach.prepare_run(since=time.time() - 30 * 86400)
        if not run_dir:
            return None
        return self.submit("Review evidence.json and targets.json and propose improvements.",
                           title=f"Reflection Coach: {n} task", workdir=run_dir, agent_pref="claude",
                           kind="reflection", agent="claude", category="reflection",
                           route_reason="reflection coach (manual)" if manual else "reflection coach (scheduled)")

    def reply(self, task_id, message):
        """Answer a needs_input task, or send follow-up instructions to a finished one."""
        self.db.update_task(task_id, pending_message=message, status="queued", error=None, not_before=None)
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

    def merge(self, task_id):
        task = self.db.task(task_id)
        if not task.get("branch") or task.get("merged_at"):
            raise workspace.WorkspaceError("task has no unmerged branch")
        if task["status"] in ("queued", "running"):
            raise workspace.WorkspaceError("task is still running")
        head = workspace.merge(task)
        self.db.update_task(task_id, merged_at=time.time())
        self.emit(task_id, "merged", {"branch": task["branch"], "into": task["base_branch"], "head": head})
        self.changed(task_id)
        return head

    def discard_worktree(self, task_id, delete_branch=False):
        task = self.db.task(task_id)
        if not task.get("branch"):
            raise workspace.WorkspaceError("task has no worktree")
        if task["status"] in ("queued", "running"):
            raise workspace.WorkspaceError("task is still running")
        workspace.remove(task, delete_branch=delete_branch)
        self.db.update_task(task_id, exec_dir=None)
        self.emit(task_id, "worktree_removed", {"branch_deleted": delete_branch})
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
        for task in self.db.query("SELECT * FROM tasks WHERE status='queued' AND "
                                  "(not_before IS NULL OR not_before<=?) ORDER BY id", (now,)):
            self._try_start(task)
        if self.coach.due(now):
            self.run_coach()

    def _route(self, task):
        """Classification may call a local LLM (seconds), so it runs off the scheduler thread."""
        try:
            agent, cat, cx, reason = self.router.route(task)
            self.db.update_task(task["id"], agent=agent, category=cat, complexity=cx, route_reason=reason)
            self.emit(task["id"], "routed", {"agent": agent, "category": cat, "complexity": cx,
                                             "reason": reason})
        finally:
            self.routing.discard(task["id"])

    def _lock_key(self, task):
        """Tasks in their own worktree never collide; tasks run in place share their directory."""
        if task.get("exec_dir") and task.get("branch"):
            return f"task:{task['id']}"
        wd = task.get("workdir") or ""
        if (task.get("kind") == "task" and task.get("agent") == "claude" and wd
                and self.cfg["workspace"].get("use_worktrees") and workspace.repo_root(wd)):
            return f"task:{task['id']}"  # will get a worktree
        return wd or f"task:{task['id']}"

    def _try_start(self, task):
        if task["id"] in self.routing:
            return
        if not task.get("agent"):
            self.routing.add(task["id"])
            threading.Thread(target=self._route, args=(task,), daemon=True).start()
            return
        key = self._lock_key(task)
        with self.lock:
            if key in self.busy.values():
                return
        account, slot = None, False
        if task["agent"] == "claude":
            account, slot = self.pool.acquire(), True
        elif task["agent"] == "quick":
            account = self.pool.best_available()
        if task["agent"] in ("claude", "quick") and not account:
            return  # all accounts busy, cooling down or parked; stay queued
        with self.lock:
            self.busy[task["id"]] = key
        self.db.update_task(task["id"], status="running", started_at=task.get("started_at") or time.time(),
                            attempts=(task.get("attempts") or 0) + 1, last_event_at=time.time(),
                            not_before=None)
        self.stall_flagged.discard(task["id"])
        self.changed(task["id"])
        threading.Thread(target=self._guard, args=(task, account, slot), daemon=True).start()

    def _guard(self, task, account, slot):
        try:
            if task["agent"] == "claude":
                task = self._prepare_workspace(task)
                self._run_claude(task, account)
            elif task["agent"] == "quick":
                self._run_quick(task, account)
            else:
                self._run_local(task)
        except Exception as e:
            traceback.print_exc()
            self.db.update_task(task["id"], status="failed", error=f"OtterRaft error: {e}",
                                finished_at=time.time())
            self.emit(task["id"], "error", {"text": str(e)})
        finally:
            if slot:
                self.pool.release(account["name"])
            with self.lock:
                self.busy.pop(task["id"], None)
            self.active.pop(task["id"], None)
            self.changed(task["id"])

    def _prepare_workspace(self, task):
        if task.get("exec_dir"):
            return task
        fields = None
        if not task.get("workdir"):
            # Never let an agent loose in OtterRaft's own directory.
            wd = Path(self.cfg["data_dir"]) / "workspaces" / f"task-{task['id']}"
            wd.mkdir(parents=True, exist_ok=True)
            fields = {"workdir": str(wd), "exec_dir": str(wd)}
        elif task.get("kind") == "task":
            fields = workspace.prepare(self.cfg, task)
            if fields:
                self.emit(task["id"], "worktree", {"branch": fields["branch"], "path": fields["exec_dir"],
                                                   "base": fields["base_branch"]})
        self.db.update_task(task["id"], **(fields or {"exec_dir": task["workdir"]}))
        return self.db.task(task["id"])

    # -- Claude --------------------------------------------------------------------------
    def _system_prompt(self, task):
        if task.get("kind") == "reflection":
            return COACH_SYSTEM
        lessons = self.learner.relevant_lessons(task["prompt"], task.get("workdir"))
        if lessons:
            self.emit(task["id"], "lessons", {"ids": [l["id"] for l in lessons],
                                              "texts": [l["text"] for l in lessons]})
        return "\n\n".join(x for x in (REPORT_INSTRUCTIONS, self.learner.lessons_block(lessons)) if x)

    def _run_claude(self, task, account):
        tid = task["id"]
        message, resume = task.get("pending_message"), None
        if task.get("session_id") and message:
            prompt, resume = message, task["session_id"]
            prev = next((a for a in self.cfg["accounts"] if a["name"] == task.get("account")), None)
            if prev and prev["name"] != account["name"]:
                if handoff_session(resume, prev, account):
                    self.emit(tid, "handoff", {"from": prev["name"], "to": account["name"]})
                else:
                    self.emit(tid, "handoff_failed", {"from": prev["name"], "to": account["name"]})
                    resume = None
        if not resume:
            prompt = task["prompt"]
            if message:
                prompt += RESTART_NOTE + ("" if message == CONTINUE_MSG else f"\n\n{message}")
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

        extra = ["--tools", "Read,Grep,Glob"] if task.get("kind") == "reflection" else []
        # A resumed session keeps its original system prompt, so only a fresh run builds one.
        system = None if resume else self._system_prompt(task)
        run = ClaudeRun(self.cfg, account, prompt, task.get("exec_dir") or task.get("workdir"),
                        system, on_event, resume_session=resume, extra_args=extra,
                        model=self._agent_model(task))
        self.active[tid] = run
        res = run.run()
        if run.cancelled:
            return
        cur = self.db.task(tid)
        self.db.update_task(
            tid, cost_usd=(cur["cost_usd"] or 0) + res["cost_usd"],
            input_tokens=(cur["input_tokens"] or 0) + res["input_tokens"],
            output_tokens=(cur["output_tokens"] or 0) + res["output_tokens"],
            cached_tokens=(cur["cached_tokens"] or 0) + res["cached_tokens"],
            duration_ms=(cur["duration_ms"] or 0) + res["duration_ms"],
            num_turns=(cur["num_turns"] or 0) + res["num_turns"],
            session_id=res["session_id"], model=res["model"])
        self.db.add_usage(task_id=tid, kind="claude", name=account["name"], model=res["model"],
                          cost_usd=res["cost_usd"], input_tokens=res["input_tokens"],
                          output_tokens=res["output_tokens"], cached_tokens=res["cached_tokens"],
                          duration_ms=res["duration_ms"], ok=1 if res["ok"] else 0,
                          category=task.get("category"))
        self.learner.record_permission_denials(tid, res["permission_denials"])
        if res["permission_denials"]:
            self.emit(tid, "permission_denials", {"items": res["permission_denials"][:20]})
        if not res["ok"] and self._recover(self.db.task(tid), account, res, message):
            return
        self._finish(self.db.task(tid), res["ok"], res["text"], res["error"])

    def _agent_model(self, task):
        """Light tasks run on the lighter model; the rest on the configured (or account) default."""
        c = self.cfg["claude"]
        if task.get("kind") == "task" and c.get("light_model") and \
                (task.get("complexity") or 5) <= c.get("light_max_complexity", 3):
            return c["light_model"]
        return c.get("model") or None

    def _run_quick(self, task, account):
        tid = task["id"]
        q = self.cfg["quick"]
        message = task.get("pending_message")
        prompt = task["prompt"]
        if message:
            prompt += f"\n\nYour previous answer:\n{task.get('result_text') or ''}\n\n" \
                      f"Follow-up from the user:\n{message}"
        self.db.update_task(tid, account=account["name"], model=q.get("model"), pending_message=None)
        self.emit(tid, "start", {"agent": "quick", "account": account["name"], "model": q.get("model")})
        lessons = self.learner.relevant_lessons(task["prompt"], task.get("workdir"))
        system = "\n\n".join(x for x in (QUICK_SYSTEM, self.learner.lessons_block(lessons)) if x)
        res = quick_call(self.cfg, account, system, prompt, q.get("model"), timeout=q.get("timeout_sec", 300))
        cur = self.db.task(tid)
        self.db.update_task(
            tid, model=res["model"], cost_usd=(cur["cost_usd"] or 0) + res["cost_usd"],
            input_tokens=(cur["input_tokens"] or 0) + res["input_tokens"],
            output_tokens=(cur["output_tokens"] or 0) + res["output_tokens"],
            cached_tokens=(cur["cached_tokens"] or 0) + res["cached_tokens"],
            duration_ms=(cur["duration_ms"] or 0) + res["duration_ms"])
        self.db.add_usage(task_id=tid, kind="quick", name=account["name"], model=res["model"],
                          cost_usd=res["cost_usd"], input_tokens=res["input_tokens"],
                          output_tokens=res["output_tokens"], cached_tokens=res["cached_tokens"],
                          duration_ms=res["duration_ms"], ok=None if res["ok"] else 0,  # review decides
                          category=task.get("category"))
        self.emit(tid, "text", {"text": res["text"][:4000]} if res["ok"] else {"text": f"error: {res['error']}"})
        if res["ok"]:
            return self._finish(self.db.task(tid), True, res["text"], None)
        if self._recover(self.db.task(tid), account, res, message, resumable=False):
            return
        # Anything else (e.g. the answer needs files after all): hand it to a full agent.
        self.db.update_task(tid, status="queued", agent="claude", agent_pref="claude", pending_message=message,
                            route_reason=f"escalated from quick answer: {(res['error'] or '')[:200]}")
        self.emit(tid, "escalated", {"to": "claude", "reason": res["error"]})

    def _recover(self, task, account, res, message, resumable=True):
        """Handle a failure that is not the task's fault. Returns True when the task was requeued."""
        tid, name, kind = task["id"], account["name"], res["failure"]
        if kind == "quota":
            until = self.pool.cool_down(name, res["reset_at"] or self.pool.window_reset(name), res["error"] or "")
            self.emit(tid, "account_limit", {"account": name, "until": until})
            self.notify("Account limit", f"{name} is out of usage until {time.ctime(until)}; switching account")
        elif kind == "login":
            self.pool.park_login(name)
            self.emit(tid, "account_login_required", {"account": name})
            self.notify("Account logged out", f"{name} needs `otterraft login {name}`")
        elif kind == "transient":
            n = (task.get("retries") or 0) + 1
            if n > self.cfg["claude"].get("transient_retries", 5):
                return False
            delay = min(self.cfg["claude"].get("transient_backoff_sec", 30) * 2 ** (n - 1), 900)
            self.db.update_task(tid, retries=n, not_before=time.time() + delay)
            self.emit(tid, "retry_later", {"attempt": n, "in_sec": delay, "error": (res["error"] or "")[:300]})
        elif kind == "bad_session":
            if (task.get("session_resets") or 0) >= 1:
                return False
            # The transcript cannot be resumed (see Paperclip's poisoned-session guard): start over.
            self.db.update_task(tid, status="queued", session_id=None, pending_message=message,
                                session_resets=(task.get("session_resets") or 0) + 1)
            self.emit(tid, "session_reset", {"error": (res["error"] or "")[:300]})
            return True
        else:
            return False
        # Resume where it stopped if the run got anywhere; otherwise resend what it was given.
        progressed = resumable and res["num_turns"] > 0 and res["session_id"]
        self.db.update_task(tid, status="queued", pending_message=CONTINUE_MSG if progressed else message)
        return True

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
            to = "quick" if self.cfg["quick"].get("enabled") else "claude"
            self.db.update_task(tid, status="queued", agent=to, agent_pref=to,
                                route_reason=f"escalated from local: {err}")
            self.emit(tid, "escalated", {"to": to, "reason": err})
            return
        self._finish(self.db.task(tid), ok, text, err)

    # -- completion ----------------------------------------------------------------------
    def _finish(self, task, ok, text, error):
        if task.get("kind") == "reflection":
            return self._finish_reflection(task, ok, text, error)
        tid = task["id"]
        report = parse_report(text) or ({"status": "done" if ok else "failed",
                                         "summary": strip_report(text)[:1500],
                                         "output": [], "test_cases": []} if text else None)
        changed_files, diffstat = None, None
        if task.get("branch") and task.get("exec_dir"):
            try:
                changed_files, diffstat = workspace.commit(task)
            except workspace.WorkspaceError as e:
                self.emit(tid, "error", {"text": f"could not commit the worktree: {e}"})
        elif task.get("agent") == "claude":
            changed_files = git_changes(task.get("exec_dir") or task.get("workdir"))
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
                            diffstat=diffstat, progress=progress, finished_at=time.time())
        self.emit(tid, "finished", {"status": status})
        task = self.db.task(tid)
        lessons = self.learner.reflect(task)
        if lessons:
            self.emit(tid, "learned", {"lesson_ids": lessons})
        label = {"review": "ready for review", "needs_input": "needs your input"}.get(status, status)
        self.notify(f"Task #{tid} {label}", (report or {}).get("summary", error or "")[:300])

    def _finish_reflection(self, task, ok, text, error):
        tid = task["id"]
        summary, n = self.coach.ingest(tid, text, task["workdir"]) if ok else (error or "failed", 0)
        report = {"status": "done" if ok else "failed", "summary": summary, "output":
                  [f"{n} proposal(s) waiting for your approval in the Brain tab"], "test_cases": []}
        self.db.update_task(tid, status="review" if ok else "failed", report=report,
                            result_text=strip_report(text), error=None if ok else error,
                            progress=1.0 if ok else 0, finished_at=time.time())
        self.emit(tid, "proposals", {"count": n})
        if n:
            self.notify("Reflection Coach", f"{n} improvement proposal(s) to review")
        self.bus.publish({"type": "proposals"})


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
        p = subprocess.run(task["verify_cmd"], shell=True, cwd=task.get("exec_dir") or task.get("workdir") or None,
                           capture_output=True, text=True, timeout=900)
        out = (p.stdout + p.stderr)[-4000:]
        return {"cmd": task["verify_cmd"], "ok": p.returncode == 0, "code": p.returncode,
                "output": out, "sec": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired:
        return {"cmd": task["verify_cmd"], "ok": False, "code": None, "output": "timeout", "sec": 900}
