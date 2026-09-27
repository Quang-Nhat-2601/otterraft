#!/usr/bin/env python3
"""Stand-in for the `claude` CLI that speaks stream-json. Behaviour is picked by the account's
CLAUDE_CONFIG_DIR name and the prompt:
- config dir containing 'limited'   -> starts work, then fails with a usage-limit error
- config dir containing 'loggedout' -> fails with a login error
- prompt containing 'OVERLOAD'      -> first run fails with 529 overloaded, later runs succeed
- --resume of an unknown session    -> 'No conversation found'
- --tools Read,Grep,Glob            -> behaves as the Reflection Coach
Every invocation's arguments are appended to $FAKE_CLAUDE_LOG when set."""
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
if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
        f.write(json.dumps({"args": args, "cwd": os.getcwd(), "config": str(cfg_dir)}) + "\n")


def fail(text, turns=0):
    out({"type": "result", "subtype": "success", "is_error": True, "session_id": sid, "result": text,
         "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1}, "num_turns": turns})
    sys.exit(1)


if "loggedout" in str(cfg_dir):
    fail("Invalid API key · Please run /login")

proj = cfg_dir / "projects" / os.getcwd().replace("/", "-")
if resume and not (proj / f"{sid}.jsonl").exists():
    fail(f"No conversation found with session ID: {sid}")
proj.mkdir(parents=True, exist_ok=True)
(proj / f"{sid}.jsonl").write_text(json.dumps({"prompt": prompt}) + "\n")

util = 0.97 if "limited" in str(cfg_dir) else 0.3
out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": util, "resetsAt": 4102444800},
    "seven_day": {"utilization": 0.5, "resetsAt": 4102444800}}}})
out({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-test",
     "permissionMode": args[args.index("--permission-mode") + 1]})

if "--tools" in args:  # Reflection Coach
    evidence = json.loads(Path("evidence.json").read_text())
    ids = [e["id"] for e in evidence]
    block = {"summary": f"looked at {len(ids)} tasks", "proposals": [
        {"title": "Always run the test suite", "target": "global", "action": "append",
         "rationale": "verify failed twice", "evidence": ids[:2],
         "content": "## Testing\n- Run `pytest -q` before reporting done."},
        {"title": "No evidence", "target": "global", "action": "append", "rationale": "x",
         "evidence": [], "content": "## Nothing"},
        {"title": "Deploy skill", "target": "skill:deploy-checklist", "action": "create",
         "rationale": "deploys forgot migrations", "evidence": ids[:1],
         "content": "---\nname: deploy-checklist\ndescription: Use before deploying.\n---\n\n1. Run migrations."}]}
    text = "Done.\n```coach-proposals\n" + json.dumps(block) + "\n```"
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": text,
         "total_cost_usd": 0.05, "usage": {"input_tokens": 10, "output_tokens": 10}, "num_turns": 2})
    sys.exit(0)

if "OVERLOAD" in prompt:
    marker = cfg_dir / "overloaded-once"
    if not marker.exists():
        marker.write_text("1")
        fail('API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}')

out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {
    "todos": [{"content": "Read code", "status": "completed"}, {"content": "Fix bug", "status": "in_progress"}]}}]}})
if "limited" in str(cfg_dir):
    fail("Claude AI usage limit reached|4102444800", turns=1)
Path("fix.txt").write_text(f"fixed by {cfg_dir.name}\n")
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
     "modelUsage": {"claude-test": {"inputTokens": 1000, "outputTokens": 200, "cacheReadInputTokens": 500,
                                    "cacheCreationInputTokens": 100},
                    "claude-haiku-test": {"inputTokens": 300, "outputTokens": 20}},
     "num_turns": 3, "duration_ms": 1234,
     "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}},
                            {"tool_name": "Bash", "tool_input": {"command": "npm test -- --watch=false"}}]})
