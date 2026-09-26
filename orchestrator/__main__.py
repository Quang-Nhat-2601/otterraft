"""CLI: python -m orchestrator {init,serve,add,accounts,login,doctor}"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .accounts import sync_shared
from .bus import Bus
from .db import DB

EXAMPLE = Path(__file__).resolve().parent.parent / "orchestrator.example.json"


def open_db(cfg):
    return DB(Path(cfg["data_dir"]) / "orchestrator.db")


def cmd_init(args, _cfg):
    dst = Path(args.config)
    if dst.exists():
        print(f"{dst} already exists")
        return
    shutil.copy(EXAMPLE, dst)
    print(f"Wrote {dst}. Edit accounts/models, then run: python -m orchestrator login <account>")


def cmd_serve(args, cfg):
    from .runner import Orchestrator
    from .server import serve
    orch = Orchestrator(cfg, open_db(cfg), Bus())
    orch.start()
    httpd = serve(orch)
    for line in sync_shared(cfg):
        print(line)
    print(f"Dashboard: http://{cfg['host']}:{cfg['port']}/?token={cfg['auth_token']}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        orch.stop.set()


def cmd_add(args, cfg):
    db = open_db(cfg)
    prompt = args.prompt if args.prompt != "-" else sys.stdin.read()
    tid = db.create_task(prompt=prompt, title=args.title or prompt.strip().split("\n")[0][:80],
                         workdir=os.path.abspath(args.workdir) if args.workdir else "",
                         agent_pref=args.agent, verify_cmd=args.verify or "", status="queued")
    print(f"Queued task #{tid}")


def cmd_accounts(args, cfg):
    db = open_db(cfg)
    for a in cfg["accounts"]:
        st = db.one("SELECT * FROM account_state WHERE name=?", (a["name"],)) or {}
        cd = st.get("cooldown_until") or 0
        state = f"cooling down until {time.ctime(cd)}" if cd > time.time() else "ready"
        logged = _logged_in(a)
        print(f"{a['name']:<12} prio={a.get('priority', 9)} dir={a.get('config_dir') or '~/.claude'} "
              f"login={'yes' if logged else 'NO'} {state}")


def _logged_in(acc):
    """Best effort: macOS keeps tokens in the Keychain, so a missing file is not proof of logout."""
    if acc.get("config_dir"):
        root = Path(acc["config_dir"])
        return (root / ".credentials.json").exists() or (root / ".claude.json").exists()
    home = Path.home()
    return (home / ".claude" / ".credentials.json").exists() or (home / ".claude.json").exists()


def cmd_login(args, cfg):
    acc = next((a for a in cfg["accounts"] if a["name"] == args.account), None)
    if not acc:
        sys.exit(f"No account named {args.account} in {cfg['_path']}")
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    if acc.get("config_dir"):
        Path(acc["config_dir"]).mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = acc["config_dir"]
    print(f"Starting Claude Code for account '{acc['name']}'. Type /login, sign in, then /exit.")
    subprocess.call([cfg["claude_bin"]], env=env)
    for line in sync_shared(cfg):
        print(line)


def cmd_doctor(args, cfg):
    from .agents.local import LocalLLM
    ok = True
    path = shutil.which(cfg["claude_bin"])
    print(f"[{'ok' if path else 'X '}] claude CLI: {path or 'not found'}")
    ok &= bool(path)
    for a in cfg["accounts"]:
        li = _logged_in(a)
        print(f"[{'ok' if li else '??'}] account {a['name']}: {a.get('config_dir') or '~/.claude'}"
              + ("" if li else f"  -> run: python -m orchestrator login {a['name']}"))
    for line in sync_shared(cfg, dry_run=True):
        print(line)
    local = LocalLLM(cfg)
    if local.enabled:
        have = local.available_models()
        print(f"[{'ok' if have else 'X '}] local server {cfg['local']['ollama_url']}: "
              f"{', '.join(have) if have else 'unreachable (is `ollama serve` running?)'}")
        for m in cfg["local"]["models"]:
            inst = m["name"] in have
            print(f"[{'ok' if inst else 'X '}] model {m['name']}"
                  + ("" if inst else f"  -> run: ollama pull {m['name']}"))
    else:
        print("[--] local models disabled")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(prog="orchestrator")
    ap.add_argument("--config", default=os.environ.get("ORCH_CONFIG", "orchestrator.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="write an example config")
    sub.add_parser("serve", help="run scheduler + dashboard")
    a = sub.add_parser("add", help="queue a task ('-' reads the prompt from stdin)")
    a.add_argument("prompt")
    a.add_argument("-w", "--workdir")
    a.add_argument("-t", "--title")
    a.add_argument("-a", "--agent", choices=["auto", "claude", "local"], default="auto")
    a.add_argument("-v", "--verify", help="shell command that must pass, e.g. 'pytest -q'")
    sub.add_parser("accounts", help="show accounts and cooldowns")
    l = sub.add_parser("login", help="log an account in (opens Claude Code with its config dir)")
    l.add_argument("account")
    sub.add_parser("doctor", help="check CLI, logins and local models")
    args = ap.parse_args()
    cfg = config.load(args.config) if args.cmd != "init" else None
    {"init": cmd_init, "serve": cmd_serve, "add": cmd_add, "accounts": cmd_accounts,
     "login": cmd_login, "doctor": cmd_doctor}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
