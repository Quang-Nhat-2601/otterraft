"""Claude account pool: picks an account that is enabled, not cooling down, and has a free slot."""
import json
import os
import shutil
import threading
import time
from pathlib import Path


class AccountPool:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.lock = threading.Lock()
        self.running = {}  # account name -> count
        self.login_rest_until = {}  # account name -> end of the rest after a first login failure

    @property
    def accounts(self):
        return [a for a in self.cfg["accounts"] if a.get("enabled", True)]

    def state(self, name):
        return self.db.one("SELECT * FROM account_state WHERE name=?", (name,)) or \
            {"name": name, "cooldown_until": 0, "last_error": None}

    def utilization(self, name):
        """Latest 5-hour window utilization (0-1) reported by Claude Code, or None."""
        st = self.state(name)
        limits = st.get("limits") or {}
        win = (limits.get("unifiedWindows") or {}).get("five_hour") or {}
        if win.get("resetsAt") and win["resetsAt"] < time.time():
            return 0.0  # window already rolled over
        return win.get("utilization")

    def available(self, exclude=()):
        """Usable accounts, best first: under the switch threshold, then by priority."""
        now = time.time()
        threshold = self.cfg["claude"].get("switch_at_utilization", 0.9)
        out = []
        for a in self.accounts:
            if a["name"] in exclude or self.state(a["name"])["cooldown_until"] > now:
                continue
            out.append(a)
        return sorted(out, key=lambda a: ((self.utilization(a["name"]) or 0) >= threshold,
                                          a.get("priority", 9)))

    def window_reset(self, name):
        """When the account's exhausted usage window resets, from the latest rate-limit report."""
        limits = self.state(name).get("limits") or {}
        now = time.time()
        full = [w.get("resetsAt") for w in (limits.get("unifiedWindows") or {}).values()
                if (w.get("utilization") or 0) >= 0.99 and (w.get("resetsAt") or 0) > now]
        if full:
            return max(full)
        if limits.get("status") not in (None, "allowed", "allowed_warning") and (limits.get("resetsAt") or 0) > now:
            return limits["resetsAt"]
        return None

    def record_limits(self, name, info):
        """Store a rate_limit_event; a non-'allowed' status puts the account on cooldown."""
        self.db.execute(
            "INSERT INTO account_state (name, limits, limits_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET limits=excluded.limits, limits_at=excluded.limits_at",
            (name, json.dumps(info), time.time()))
        if info.get("status") and info["status"] not in ("allowed", "allowed_warning"):
            self.cool_down(name, info.get("resetsAt"), f"rate limit status: {info['status']}")

    def best_available(self):
        """The account to use for a short call (routing, quick answers). Takes no parallel slot:
        these calls last seconds and must not wait behind a long agent run."""
        accounts = self.available()
        return accounts[0] if accounts else None

    def park_login(self, name):
        """Take a logged-out account out of rotation until `otterraft login <name>`."""
        return self.cool_down(name, time.time() + 10 * 365 * 86400,
                              f"login required: run `otterraft login {name}`")

    def login_failed(self, name):
        """The CLI sometimes reports "Login expired" once on a valid token: rest the account 15 s,
        and park it on a login failure within 10 minutes after that rest. Failures during the rest
        are the same stale token hit by concurrent calls, not a second strike. Returns True when parked."""
        now = time.time()
        with self.lock:  # the DB writes too: a late 15 s rest must never overwrite a park
            rest_end = self.login_rest_until.get(name, 0)
            if now < rest_end:
                return False
            if now < rest_end + 600:
                self.park_login(name)
                return True
            self.login_rest_until[name] = now + 15
            self.cool_down(name, now + 15, "login check failed once; retrying shortly")
            return False

    def acquire(self, exclude=()):
        """Reserve a slot on the best account. Returns the account dict or None."""
        with self.lock:
            for a in self.available(exclude):
                if self.running.get(a["name"], 0) < a.get("max_parallel", 1):
                    self.running[a["name"]] = self.running.get(a["name"], 0) + 1
                    return a
        return None

    def release(self, name):
        with self.lock:
            self.running[name] = max(0, self.running.get(name, 0) - 1)

    def cool_down(self, name, reset_epoch=None, reason=""):
        until = reset_epoch or time.time() + self.cfg["claude"]["default_cooldown_min"] * 60
        self.db.execute(
            "INSERT INTO account_state (name, cooldown_until, last_error) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET cooldown_until=excluded.cooldown_until, "
            "last_error=excluded.last_error", (name, until, reason[:500]))
        return until

    def reset(self, name):
        self.login_rest_until.pop(name, None)
        self.db.execute("UPDATE account_state SET cooldown_until=0, last_error=NULL WHERE name=?", (name,))

    def next_free_at(self):
        """Earliest time any account comes out of cooldown (for the UI)."""
        times = [self.state(a["name"])["cooldown_until"] for a in self.accounts]
        return min(times) if times else None


# Everything that shapes how Claude Code works, except the login itself.
SHARED_ITEMS = ["skills", "agents", "commands", "output-styles", "plugins", "CLAUDE.md", "settings.json"]


def _main_state_file(shared_dir):
    """~/.claude.json lives next to ~/.claude by default, inside the dir when CLAUDE_CONFIG_DIR is set."""
    inside = Path(shared_dir) / ".claude.json"
    return inside if inside.exists() else Path(shared_dir).parent / ".claude.json"


def _link_without_symlink_rights(s, d):
    """Windows without Developer Mode: a junction (dirs) or hard link (files) needs no admin and
    stays live. A copy is the last resort; it goes stale and later runs report it as the account's own."""
    try:
        if s.is_dir() and os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(s), str(d))
        else:
            os.link(s, d)
    except OSError:
        (shutil.copytree if s.is_dir() else shutil.copy2)(s, d)


def sync_shared(cfg, dry_run=False):
    """Link the shared config into each account's CLAUDE_CONFIG_DIR, so every account sees the
    same skills, CLAUDE.md, hooks, plugins and MCP servers. Never overwrites a real file or
    folder the account already has; reports it instead. Returns human-readable lines."""
    src = Path(cfg["shared_config_dir"])
    out = []
    if not src.is_dir():
        return [f"[--] shared config dir {src} not found; nothing to share"]
    for acc in cfg["accounts"]:
        if not acc.get("config_dir"):
            continue
        dst = Path(acc["config_dir"])
        if dst.resolve() == src.resolve():
            continue
        linked, conflicts, missing = [], [], []
        for item in SHARED_ITEMS:
            s, d = src / item, dst / item
            if not s.exists():
                continue
            if d.exists() and os.path.samefile(d, s):  # symlink, junction or hard link
                linked.append(item)
            elif d.exists() or d.is_symlink():
                conflicts.append(item)
            elif dry_run:
                missing.append(item)
            else:
                dst.mkdir(parents=True, exist_ok=True)
                try:
                    d.symlink_to(s, target_is_directory=s.is_dir())
                except OSError:
                    _link_without_symlink_rights(s, d)
                linked.append(item)
        mcp = _sync_mcp(src, dst, dry_run)
        mark = "ok" if not missing and not conflicts else "??"
        line = f"[{mark}] account {acc['name']}: shared {', '.join(linked) or 'nothing'}"
        if mcp:
            line += f"; MCP servers: {mcp}"
        if missing:
            line += f"; not linked yet: {', '.join(missing)} (run serve/login to link)"
        if conflicts:
            line += f"; has its own {', '.join(conflicts)} (left untouched, merge by hand)"
        out.append(line)
    return out


def _sync_mcp(src, dst, dry_run):
    """Copy user-level mcpServers into the account's .claude.json (created by /login).
    Only that key is touched; the file also holds the account's own identity."""
    s_file, d_file = _main_state_file(src), dst / ".claude.json"
    if not s_file.exists() or not d_file.exists():
        return ""
    try:
        servers = json.loads(s_file.read_text(encoding="utf-8")).get("mcpServers") or {}
        state = json.loads(d_file.read_text(encoding="utf-8"))
    except ValueError:
        return "unreadable .claude.json"
    if not servers:
        return ""
    have = state.get("mcpServers") or {}
    new = {k: v for k, v in servers.items() if k not in have}
    if new and not dry_run:
        state["mcpServers"] = {**have, **new}
        tmp = d_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, d_file)
    return f"{len(servers)} ({len(new)} {'to add' if dry_run else 'added'})"
