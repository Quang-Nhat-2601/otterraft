"""Runs Claude Code headless (`claude -p --output-format stream-json`) and streams its events."""
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ..failures import classify


def cli(cfg):
    """claude_bin as an argv prefix; a list lets Windows run a script, e.g. [python, fake_claude.py]."""
    b = cfg["claude_bin"]
    return list(b) if isinstance(b, list) else [b]


def account_env(account):
    env = dict(os.environ)
    # Agents can read their environment; the brain's key reaches the brain only via account["env"].
    env.pop("OTTERRAFT_BRAIN_API_KEY", None)
    # An API key in the environment would silently override the account's subscription login.
    if not account.get("use_api_key"):
        env.pop("ANTHROPIC_API_KEY", None)
    if account.get("config_dir"):
        env["CLAUDE_CONFIG_DIR"] = account["config_dir"]
    for k, v in (account.get("env") or {}).items():
        env[k] = v
    return env


def _prompt_file(cfg, text):
    """System prompts go through a file too: Windows caps a whole command line at ~32K chars,
    and the lessons block grows. The caller deletes the file once the CLI has exited."""
    d = Path(cfg["data_dir"]) / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{uuid.uuid4().hex}.md"
    f.write_text(text, encoding="utf-8")
    return f


def _config_root(account):
    return Path(account.get("config_dir") or os.path.expanduser("~/.claude"))


def handoff_session(session_id, src_account, dst_account):
    """Copy a session transcript to another account so it can be --resume'd there."""
    src_root, dst_root = _config_root(src_account), _config_root(dst_account)
    if src_root == dst_root:
        return True
    found = list((src_root / "projects").glob(f"*/{session_id}.jsonl"))
    if not found:
        return False
    src = found[0]
    dst = dst_root / "projects" / src.parent.name / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


class ClaudeRun:
    """One invocation of the Claude CLI. Call .run(); .cancel() from another thread."""

    def __init__(self, cfg, account, prompt, workdir, system_append, on_event,
                 resume_session=None, model=None, extra_args=()):
        self.cfg = cfg
        self.account = account
        self.prompt = prompt
        self.workdir = workdir
        self.system_append = system_append
        self.on_event = on_event
        self.resume_session = resume_session
        self.model = model or cfg["claude"].get("model") or ""
        self.extra_args = list(extra_args)
        self.proc = None
        self.cancelled = False

    def command(self, system_file=None):
        c = self.cfg["claude"]
        # The prompt goes through stdin: Windows caps a whole command line at ~32K chars.
        cmd = [*cli(self.cfg), "-p", "--output-format", "stream-json",
               "--verbose", "--permission-mode", c["permission_mode"]]
        if c.get("max_turns"):
            cmd += ["--max-turns", str(c["max_turns"])]
        if c.get("max_budget_usd"):
            cmd += ["--max-budget-usd", str(c["max_budget_usd"])]
        if self.model:
            cmd += ["--model", self.model]
        if c.get("allowed_tools"):
            cmd += ["--allowedTools", ",".join(c["allowed_tools"])]
        if self.resume_session:
            # The session already carries its system prompt; sending it again costs thousands
            # of tokens per resume and changes nothing.
            cmd += ["--resume", self.resume_session]
        elif system_file:
            cmd += ["--append-system-prompt-file", str(system_file)]
        return cmd + list(c.get("extra_args") or []) + self.extra_args

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def run(self):
        system_file = _prompt_file(self.cfg, self.system_append) \
            if self.system_append and not self.resume_session else None
        try:
            return self._run(system_file)
        finally:
            if system_file:
                system_file.unlink(missing_ok=True)

    def _run(self, system_file):
        res = {"ok": False, "session_id": self.resume_session, "text": "", "cost_usd": 0.0,
               "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "duration_ms": 0,
               "num_turns": 0, "failure": None, "limit": False, "reset_at": None, "error": None,
               "permission_denials": [],
               "model": self.model, "todos": None}
        t0 = time.time()
        stderr_lines = []
        try:
            self.proc = subprocess.Popen(
                self.command(system_file), cwd=self.workdir or None, env=account_env(self.account),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            res["error"] = f"Could not start the Claude CLI ({self.cfg['claude_bin']}): {e}"
            return res
        if self.cancelled:  # cancel() ran before there was a process to stop
            self.proc.kill()

        def pump_stderr():
            for line in self.proc.stderr:
                stderr_lines.append(line.rstrip())
                del stderr_lines[:-50]

        def feed_stdin():
            # Its own thread: a prompt bigger than the pipe buffer would block here while the
            # CLI blocks writing stdout that nobody reads yet.
            try:
                self.proc.stdin.write(self.prompt)
                self.proc.stdin.close()
            except OSError:
                pass  # the CLI exited early; its stderr says why
        threading.Thread(target=pump_stderr, daemon=True).start()
        threading.Thread(target=feed_stdin, daemon=True).start()

        last_text, cli_error, n_texts, empty_result = "", "", 0, False
        tasks = {}  # for TaskCreate/TaskUpdate style progress
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.on_event("log", {"text": line[:2000]})
                continue
            t = msg.get("type")
            if t == "system" and msg.get("subtype") == "init":
                res["session_id"] = msg.get("session_id") or res["session_id"]
                res["model"] = msg.get("model") or res["model"]
                self.on_event("init", {"session_id": res["session_id"], "model": res["model"],
                                       "account": self.account["name"],
                                       "permission_mode": msg.get("permissionMode")})
            elif t == "assistant":
                # The CLI's own errors ("Login expired", "You've hit your limit") come as <synthetic> messages.
                synthetic = (msg.get("message") or {}).get("model") == "<synthetic>"
                for block in (msg.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        last_text, n_texts = block["text"], n_texts + 1
                        if synthetic:
                            cli_error = block["text"]
                        self.on_event("text", {"text": block["text"][:4000]})
                    elif block.get("type") == "tool_use":
                        name, inp = block.get("name"), block.get("input") or {}
                        self.on_event("tool", {"name": name, "input": _short(inp)})
                        todos = _todos_from_tool(name, inp, tasks)
                        if todos is not None:
                            res["todos"] = todos
                            self.on_event("todos", {"todos": todos})
            elif t == "user":
                for block in (msg.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result" \
                            and block.get("is_error"):
                        self.on_event("tool_error", {"text": _text_of(block.get("content"))[:1500]})
            elif t == "result":
                tokens = usage_totals(msg)
                empty_result = not msg.get("result")
                res.update(
                    ok=not msg.get("is_error") and msg.get("subtype") == "success",
                    text=msg.get("result") or last_text,
                    cost_usd=msg.get("total_cost_usd") or 0.0,
                    **tokens,
                    num_turns=msg.get("num_turns") or 0,
                    duration_ms=msg.get("duration_ms") or 0,
                    session_id=msg.get("session_id") or res["session_id"],
                    permission_denials=msg.get("permission_denials") or [],
                )
                if not res["ok"]:
                    res["error"] = msg.get("result") or msg.get("subtype") or "error"
                self.on_event("result", {"ok": res["ok"], "subtype": msg.get("subtype"),
                                         "cost_usd": res["cost_usd"], "turns": res["num_turns"]})
            elif t == "rate_limit_event":
                self.on_event("rate_limit", msg.get("rate_limit_info") or {})

        self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()
        res["duration_ms"] = res["duration_ms"] or int((time.time() - t0) * 1000)
        if self.cancelled:
            res.update(ok=False, error="cancelled")
            return res
        if self.proc.returncode and not res["error"]:
            res["error"] = "\n".join(stderr_lines[-10:]) or f"exit code {self.proc.returncode}"
        if not res["ok"]:
            # Never the model's own prose: a task *about* login errors must not park an account.
            # Fallback in case a CLI error isn't marked <synthetic>: the run's only text, reported
            # with an empty result (a model that wrote prose and then crashed sends no result at all).
            if not cli_error and empty_result and n_texts == 1 and res["num_turns"] <= 1:
                cli_error = last_text
            for text in (res["error"], cli_error, "\n".join(stderr_lines[-5:])):
                kind, reset = classify(text, resumed=bool(self.resume_session), num_turns=res["num_turns"])
                if kind:
                    res.update(failure=kind, reset_at=reset, limit=kind == "quota")
                    break
        return res


def usage_totals(result_msg):
    """Token totals for a run. The per-model ledger (modelUsage) also counts subagents and
    background calls that the top-level `usage` misses; fall back to `usage` when absent."""
    by_model = result_msg.get("modelUsage") or {}
    if by_model:
        t = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        for m in by_model.values():
            t["input_tokens"] += (m.get("inputTokens") or 0) + (m.get("cacheCreationInputTokens") or 0)
            t["output_tokens"] += m.get("outputTokens") or 0
            t["cached_tokens"] += m.get("cacheReadInputTokens") or 0
        return t
    u = result_msg.get("usage") or {}
    return {"input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
            "output_tokens": u.get("output_tokens") or 0,
            "cached_tokens": u.get("cache_read_input_tokens") or 0}


def _todos_from_tool(name, inp, tasks):
    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
        return [{"content": t.get("content") or t.get("activeForm", ""),
                 "status": t.get("status", "pending")} for t in inp["todos"]]
    if name == "TaskCreate":
        key = str(len(tasks) + 1)
        tasks[key] = {"content": inp.get("subject") or inp.get("description", ""), "status": "pending"}
        return list(tasks.values())
    if name == "TaskUpdate" and str(inp.get("taskId")) in tasks:
        if inp.get("status"):
            tasks[str(inp["taskId"])]["status"] = inp["status"]
        return list(tasks.values())
    return None


def _short(inp, limit=600):
    s = json.dumps(inp, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


def quick_call(cfg, account, system, prompt, model, timeout=120, on_start=None):
    """One lean Claude call on a subscription account: no tools, skills, MCP or CLAUDE.md, and
    our own short system prompt instead of Claude Code's. About 7K input tokens (measured)
    against 25K+ for even a trivial agent session, so routing and short text answers cost little.
    Returns the same result shape as ClaudeRun.run(). on_start(proc) lets a caller kill it."""
    system_file = _prompt_file(cfg, system)
    try:
        return _quick_call(cfg, account, system_file, prompt, model, timeout, on_start)
    finally:
        system_file.unlink(missing_ok=True)


def _quick_call(cfg, account, system_file, prompt, model, timeout, on_start):
    res = {"ok": False, "text": "", "error": None, "failure": None, "reset_at": None, "limit": False,
           "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
           "duration_ms": 0, "num_turns": 0, "session_id": None, "model": model,
           "permission_denials": []}
    # A neutral cwd, so no project CLAUDE.md or settings get pulled in.
    cwd = Path(cfg["data_dir"]) / "brain"
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [*cli(cfg), "-p", "--output-format", "json", "--max-turns", "1",
           "--tools", "", "--system-prompt-file", str(system_file), "--disable-slash-commands",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    if model:
        cmd += ["--model", model]
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=account_env(account), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace")
    except OSError as e:
        res["error"] = f"Could not start the Claude CLI ({cfg['claude_bin']}): {e}"
        return res
    if on_start:
        on_start(p)
    try:
        stdout, stderr = p.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        res.update(error=f"timed out after {timeout}s", failure="transient")
        return res
    res["duration_ms"] = int((time.time() - t0) * 1000)
    try:
        msg = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except ValueError:
        msg = {}
    if msg:
        res.update(usage_totals(msg), ok=not msg.get("is_error") and msg.get("subtype") == "success",
                   text=msg.get("result") or "", cost_usd=msg.get("total_cost_usd") or 0.0,
                   num_turns=msg.get("num_turns") or 0, session_id=msg.get("session_id"),
                   duration_ms=msg.get("duration_ms") or res["duration_ms"])
        by_model = msg.get("modelUsage") or {}
        if by_model:  # the model that did the work, not the background helper
            res["model"] = max(by_model, key=lambda m: by_model[m].get("outputTokens") or 0)
    if not res["ok"]:
        res["error"] = (msg.get("result") if msg else None) or stderr.strip()[-500:] or f"exit {p.returncode}"
        kind, reset = classify(res["error"])
        res.update(failure=kind, reset_at=reset, limit=kind == "quota")
    return res


def parse_json_answer(text):
    """The first JSON object in a model answer, tolerating ```json fences and chatter."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None
