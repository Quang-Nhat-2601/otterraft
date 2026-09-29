"""Configuration loading. Everything lives in one JSON file (default: ./otterraft.json)."""
import copy
import json
import os
import secrets
import shutil
from pathlib import Path

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8787,
    # The API always requires a token (?token=... or "Authorization: Bearer ..."); when empty,
    # a random one is generated once and kept in <data_dir>/token.
    "auth_token": "",
    "data_dir": "~/.otterraft",
    "claude_bin": "claude",
    # Skills, agents, commands, plugins, CLAUDE.md, settings.json and MCP servers from this
    # config dir are linked into every account's config dir, so all accounts behave the same.
    "shared_config_dir": "~/.claude",
    # Claude accounts. Each one is a separate CLAUDE_CONFIG_DIR that you logged into once.
    # config_dir = null means the default ~/.claude.
    "accounts": [
        {"name": "main", "config_dir": None, "priority": 1, "max_parallel": 1, "enabled": True},
    ],
    # The "brain" classifies every task and picks who does it. "claude" = one lean Claude call
    # (~4.4K tokens, ~2s; no tools, skills, MCP or user hooks) on whichever account is free, falling
    # back to a local model and then to keyword rules. "local" or "keywords" skip Claude.
    "brain": {"provider": "claude", "model": "sonnet", "timeout_sec": 60},
    # Text-only tasks (summaries, translations, commit messages, explanations) are answered by
    # one lean Claude call instead of a full agent session.
    "quick": {"enabled": True, "model": "sonnet", "max_complexity": 3},
    "claude": {
        # "auto" = the classifier-based auto mode, never stops to ask. Other options:
        # "acceptEdits", "bypassPermissions" (only in a sandbox!), "dontAsk".
        "permission_mode": "auto",
        # Model for agent tasks. "" = the account's default. Tasks the brain rates at most
        # light_max_complexity use light_model instead, saving the stronger model's quota.
        "model": "",
        "light_model": "sonnet",
        "light_max_complexity": 3,
        "max_turns": 80,
        "max_budget_usd": 0,
        "allowed_tools": [],
        "extra_args": [],
        # Minutes an account rests after a usage-limit hit when the reset time is unknown.
        "default_cooldown_min": 60,
        # Mark a running task as "stalled" when no event arrives for this long.
        "stall_after_sec": 300,
        # Copy the session transcript to the next account and --resume there on a limit hit.
        "handoff_on_limit": True,
        # Overloaded / 429 / network errors: retry the same task after 30s, 60s, 120s, ... up to
        # this many times before giving up.
        "transient_retries": 5,
        "transient_backoff_sec": 30,
        # Prefer another account once the 5-hour window of this one is this full (0-1).
        # Claude Code reports utilization in its stream, so this switches *before* a hard stop.
        "switch_at_utilization": 0.9,
    },
    "local": {
        # Off by default: Claude does the routing and quick answers. Turn on to keep simple
        # text tasks on your machine (privacy, offline, or saving quota on bulk work).
        "enabled": False,
        "ollama_url": "http://127.0.0.1:11434",
        # One model for every role keeps a single model in RAM. "think" is passed to Ollama for
        # reasoning models ("low" keeps gpt-oss fast on CPU; false turns thinking off).
        "models": [
            {"name": "gpt-oss:20b", "roles": ["simple", "router", "reflect"], "think": "low"},
        ],
        "timeout_sec": 300,
        # Retry a failed local task on Claude automatically.
        "escalate_to_claude": True,
    },
    "router": {
        # Categories that local models may take. Everything else goes to Claude.
        "local_categories": ["summarize", "translate", "explain", "commit_message",
                             "docs", "classify", "regex", "snippet", "chat"],
        # Max complexity (1-5) a local model may take.
        "local_max_complexity": 2,
        # Stop sending a category to local when its success rate drops below this
        # (after min_samples finished tasks).
        "min_local_success": 0.6,
        "min_samples": 3,
        "use_llm_classifier": True,
    },
    "learning": {
        "enabled": True,
        "max_lessons_in_prompt": 8,
        # Per-task lessons from the local "reflect" model. Off by default: the weekly Reflection
        # Coach (a Claude run over many tasks) finds better, evidence-backed improvements.
        "llm_reflection": False,
        # Reflection Coach: every N days (0 = only when you press the button), if at least
        # coach_min_tasks tasks finished since the last run.
        "coach_every_days": 7,
        "coach_min_tasks": 5,
        "coach_max_tasks": 40,
        # Lessons written by the local model wait for your approval before they reach prompts,
        # so a bad or injected lesson cannot silently steer every later task.
        "auto_approve_llm_lessons": False,
    },
    "workspace": {
        # Run each task in its own git worktree + branch (otterraft/task-<id>) when the workdir is a
        # git repo. Tasks on the same repo then run in parallel and your checkout stays untouched
        # until you press "Merge".
        "use_worktrees": True,
        "branch_prefix": "otterraft/task-",
        # Run once in each new worktree, e.g. "npm ci" or "uv sync", so tests can run there.
        "setup_cmd": "",
    },
    "notify": {
        # e.g. "https://ntfy.sh/my-secret-topic" -> push to your phone when tasks finish.
        "ntfy_url": "",
    },
    "auto_accept": False,
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    path = Path(path or os.environ.get("OTTERRAFT_CONFIG", "otterraft.json"))
    user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = _merge(DEFAULTS, user)
    cfg["_path"] = str(path)
    data_dir = Path(os.path.expanduser(cfg["data_dir"]))
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg["data_dir"] = str(data_dir)
    if not cfg.get("auth_token"):
        tok_file = data_dir / "token"
        if not tok_file.exists():
            tok_file.write_text(secrets.token_urlsafe(24))
            tok_file.chmod(0o600)
        cfg["auth_token"] = tok_file.read_text().strip()
    cfg["shared_config_dir"] = os.path.expanduser(cfg.get("shared_config_dir") or "~/.claude")
    for acc in cfg["accounts"]:
        if acc.get("config_dir"):
            acc["config_dir"] = os.path.expanduser(acc["config_dir"])
    return cfg


def claude_command(cfg, windows=os.name == "nt"):
    """The argv prefix that starts Claude Code. The name is looked up on PATH, since process
    creation on Windows finds only .exe. An npm install there is a `claude.cmd` wrapper, and
    cmd.exe re-parses every argument, so we run the wrapped `node cli.js` when we can find it
    and the wrapper itself otherwise. A list (e.g. [python, wrapper.py]) is used as given."""
    if cfg.get("_claude_cmd"):
        return cfg["_claude_cmd"]
    if isinstance(cfg["claude_bin"], list):
        return list(cfg["claude_bin"])
    exe = shutil.which(cfg["claude_bin"]) or cfg["claude_bin"]
    cmd = [exe]
    if windows and exe.lower().endswith((".cmd", ".bat")):
        cli = Path(exe).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        node = shutil.which("node")
        if cli.exists() and node:
            cmd = [node, str(cli)]
    cfg["_claude_cmd"] = cmd
    return cmd


def persist(cfg, keys, value):
    """Write one nested setting back to the user's config file (e.g. approved allow rules)."""
    path = Path(cfg["_path"])
    user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    node = user
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    path.write_text(json.dumps(user, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
