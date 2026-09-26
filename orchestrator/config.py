"""Configuration loading. Everything lives in one JSON file (default: ./orchestrator.json)."""
import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8787,
    # If set, the dashboard/API require ?token=... or header "Authorization: Bearer ...".
    "auth_token": "",
    "data_dir": "~/.ai-orchestrator",
    "claude_bin": "claude",
    # Claude accounts. Each one is a separate CLAUDE_CONFIG_DIR that you logged into once.
    # config_dir = null means the default ~/.claude.
    "accounts": [
        {"name": "main", "config_dir": None, "priority": 1, "max_parallel": 1, "enabled": True},
    ],
    "claude": {
        # "auto" = the classifier-based auto mode, never stops to ask. Other options:
        # "acceptEdits", "bypassPermissions" (only in a sandbox!), "dontAsk".
        "permission_mode": "auto",
        "model": "",
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
        # Prefer another account once the 5-hour window of this one is this full (0-1).
        # Claude Code reports utilization in its stream, so this switches *before* a hard stop.
        "switch_at_utilization": 0.9,
    },
    "local": {
        "enabled": True,
        "ollama_url": "http://127.0.0.1:11434",
        "models": [
            {"name": "qwen2.5-coder:7b", "roles": ["simple", "router", "reflect"]},
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
        # Use the local "reflect" model to extract lessons after each task.
        "llm_reflection": True,
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
    path = Path(path or os.environ.get("ORCH_CONFIG", "orchestrator.json"))
    user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = _merge(DEFAULTS, user)
    cfg["_path"] = str(path)
    data_dir = Path(os.path.expanduser(cfg["data_dir"]))
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg["data_dir"] = str(data_dir)
    for acc in cfg["accounts"]:
        if acc.get("config_dir"):
            acc["config_dir"] = os.path.expanduser(acc["config_dir"])
    return cfg


def persist(cfg, keys, value):
    """Write one nested setting back to the user's config file (e.g. approved allow rules)."""
    path = Path(cfg["_path"])
    user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    node = user
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    path.write_text(json.dumps(user, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
