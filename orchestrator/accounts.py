"""Claude account pool: picks an account that is enabled, not cooling down, and has a free slot."""
import json
import re
import threading
import time

LIMIT_RE = re.compile(
    r"usage limit|rate.?limit|hit your limit|limit reached|out of (extra )?usage|"
    r"quota|too many requests|\b429\b|credit balance is too low", re.I)
EPOCH_RE = re.compile(r"\|(\d{10})\b")


def detect_limit(text):
    """Return (is_limit, reset_epoch_or_None) for an error/result message."""
    if not text or not LIMIT_RE.search(text):
        return False, None
    m = EPOCH_RE.search(text)
    return True, (int(m.group(1)) if m else None)


class AccountPool:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.lock = threading.Lock()
        self.running = {}  # account name -> count

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

    def record_limits(self, name, info):
        """Store a rate_limit_event; a non-'allowed' status puts the account on cooldown."""
        self.db.execute(
            "INSERT INTO account_state (name, limits, limits_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET limits=excluded.limits, limits_at=excluded.limits_at",
            (name, json.dumps(info), time.time()))
        if info.get("status") and info["status"] not in ("allowed", "allowed_warning"):
            self.cool_down(name, info.get("resetsAt"), f"rate limit status: {info['status']}")

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
        self.db.execute("UPDATE account_state SET cooldown_until=0, last_error=NULL WHERE name=?", (name,))

    def next_free_at(self):
        """Earliest time any account comes out of cooldown (for the UI)."""
        times = [self.state(a["name"])["cooldown_until"] for a in self.accounts]
        return min(times) if times else None
