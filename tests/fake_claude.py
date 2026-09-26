#!/usr/bin/env python3
"""Stand-in for the `claude` CLI that speaks stream-json. Accounts whose CLAUDE_CONFIG_DIR
contains 'limited' fail with a usage-limit error after starting work."""
import json
import os
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
resume = args[args.index("--resume") + 1] if "--resume" in args else None
cfg_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))
sid = resume or str(uuid.uuid4())
out = lambda m: print(json.dumps(m), flush=True)

proj = cfg_dir / "projects" / os.getcwd().replace("/", "-")
if resume and not (proj / f"{sid}.jsonl").exists():
    out({"type": "result", "subtype": "error_during_execution", "is_error": True,
         "result": f"No conversation found with session ID: {sid}", "session_id": sid})
    sys.exit(1)
proj.mkdir(parents=True, exist_ok=True)
(proj / f"{sid}.jsonl").write_text(json.dumps({"prompt": prompt}) + "\n")

util = 0.97 if "limited" in str(cfg_dir) else 0.3
out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": util, "resetsAt": 4102444800},
    "seven_day": {"utilization": 0.5, "resetsAt": 4102444800}}}})
out({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-test",
     "permissionMode": args[args.index("--permission-mode") + 1]})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {
    "todos": [{"content": "Read code", "status": "completed"}, {"content": "Fix bug", "status": "in_progress"}]}}]}})
if "limited" in str(cfg_dir):
    out({"type": "result", "subtype": "success", "is_error": True, "session_id": sid,
         "result": "Claude AI usage limit reached|4102444800", "total_cost_usd": 0.01,
         "usage": {"input_tokens": 10, "output_tokens": 5}, "num_turns": 1})
    sys.exit(1)
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                                                  "input": {"command": "pytest -q"}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
                                              "is_error": True, "content": "1 failed"}]}})
report = {"status": "done", "summary": "Fixed the bug" + (" after resume" if resume else ""),
          "input": prompt[:100], "output": ["app.py: handle empty email"],
          "test_cases": [{"title": "Login with blank email", "steps": ["open /login", "submit"],
                          "expected": "400 error, no crash"}], "notes": ""}
text = "All done.\n```orchestrator-report\n" + json.dumps(report) + "\n```"
out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": text,
     "total_cost_usd": 0.12, "usage": {"input_tokens": 1000, "cache_read_input_tokens": 500, "output_tokens": 200},
     "num_turns": 3, "duration_ms": 1234,
     "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}]})
