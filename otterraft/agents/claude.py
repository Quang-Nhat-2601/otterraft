"""Runs Claude Code headless (`claude -p --output-format stream-json`) and streams its events."""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..failures import classify


def account_env(account):
    env = dict(os.environ)
    # An API key in the environment would silently override the account's subscription login.
    if not account.get("use_api_key"):
        env.pop("ANTHROPIC_API_KEY", None)
    if account.get("config_dir"):
        env["CLAUDE_CONFIG_DIR"] = account["config_dir"]
    for k, v in (account.get("env") or {}).items():
        env[k] = v
    return env


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

    def command(self):
        c = self.cfg["claude"]
        # The prompt goes through stdin: Windows caps a whole command line at ~32K chars.
        cmd = [self.cfg["claude_bin"], "-p", "--output-format", "stream-json",
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
        elif self.system_append:
            cmd += ["--append-system-prompt", self.system_append]
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
        res = {"ok": False, "session_id": self.resume_session, "text": "", "cost_usd": 0.0,
               "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "duration_ms": 0,
               "num_turns": 0, "failure": None, "limit": False, "reset_at": None, "error": None,
               "permission_denials": [],
               "model": self.model, "todos": None}
        t0 = time.time()
        stderr_lines = []
        try:
            self.proc = subprocess.Popen(
                self.command(), cwd=self.workdir or None, env=account_env(self.account),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            res["error"] = f"Could not start the Claude CLI ({self.cfg['claude_bin']}): {e}"
            return res
        try:
            self.proc.stdin.write(self.prompt)
            self.proc.stdin.close()
        except OSError:
            pass  # the CLI exited early; its stderr says why

        def pump_stderr():
            for line in self.proc.stderr:
                stderr_lines.append(line.rstrip())
                del stderr_lines[:-50]
        threading.Thread(target=pump_stderr, daemon=True).start()

        last_text = ""
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
                for block in (msg.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        last_text = block["text"]
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
            # CLI errors such as "Login expired" can arrive as the only assistant message, with an empty result.
            short_reply = last_text if res["num_turns"] <= 1 else ""
            for text in (res["error"], short_reply, "\n".join(stderr_lines[-5:])):
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


def quick_call(cfg, account, system, prompt, model, timeout=120):
    """One lean Claude call on a subscription account: no tools, skills, MCP or CLAUDE.md, and
    our own short system prompt instead of Claude Code's. About 1K tokens of context instead of
    ~40K for a default `claude -p`, so routing and short text answers cost almost no quota.
    Returns the same result shape as ClaudeRun.run()."""
    res = {"ok": False, "text": "", "error": None, "failure": None, "reset_at": None, "limit": False,
           "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
           "duration_ms": 0, "num_turns": 0, "session_id": None, "model": model,
           "permission_denials": []}
    # A neutral cwd, so no project CLAUDE.md or settings get pulled in.
    cwd = Path(cfg["data_dir"]) / "brain"
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [cfg["claude_bin"], "-p", "--output-format", "json", "--max-turns", "1",
           "--tools", "", "--system-prompt", system, "--disable-slash-commands",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    if model:
        cmd += ["--model", model]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd, env=account_env(account), input=prompt,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        res.update(error=f"timed out after {timeout}s", failure="transient")
        return res
    except OSError as e:
        res["error"] = f"Could not start the Claude CLI ({cfg['claude_bin']}): {e}"
        return res
    res["duration_ms"] = int((time.time() - t0) * 1000)
    try:
        msg = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
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
        res["error"] = (msg.get("result") if msg else None) or p.stderr.strip()[-500:] or f"exit {p.returncode}"
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
