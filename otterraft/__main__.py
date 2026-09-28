"""CLI: otterraft {init,serve,add,accounts,login,doctor,bench} (or python -m otterraft ...)"""
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

EXAMPLE = Path(__file__).resolve().parent / "example.json"


def open_db(cfg):
    return DB(Path(cfg["data_dir"]) / "otterraft.db")


def cmd_init(args, _cfg):
    dst = Path(args.config)
    if dst.exists():
        print(f"{dst} already exists")
        return
    shutil.copy(EXAMPLE, dst)
    print(f"Wrote {dst}. Edit accounts/models, then run: otterraft login <account>")


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
    open_db(cfg).execute("UPDATE account_state SET cooldown_until=0, last_error=NULL WHERE name=? "
                         "AND last_error LIKE 'login required%'", (acc["name"],))
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
              + ("" if li else f"  -> run: otterraft login {a['name']}"))
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


BENCH_TEXT = (
    "Ollama runs large language models directly on a personal computer. On a machine without a GPU, "
    "generation speed depends mostly on memory bandwidth rather than on the number of CPU cores, "
    "because for every token the model has to read all of its active weights from RAM. That is why "
    "Mixture-of-Experts models, which activate only a few billion parameters per token, are usually "
    "much faster than dense models of the same size on disk. They still need enough RAM to hold all "
    "of their weights, and once the operating system starts swapping, speed collapses. When picking "
    "a model, leave at least a few gigabytes free for the operating system, the browser and the "
    "developer tools running alongside it.")
BENCH_DIFF = """--- a/app/auth.py
+++ b/app/auth.py
@@ def login(email, password):
-    user = db.find_user(email)
+    user = db.find_user(email.strip().lower())
     if not user:
-        raise Exception("no user")
+        raise AuthError("invalid email or password")"""


def cmd_bench(args, cfg):
    """Time each local model on the kinds of work OtterRaft gives it."""
    from .agents.local import LocalLLM
    from .router import CLASSIFIER_SYSTEM
    local = LocalLLM(cfg)
    sizes = local.model_sizes()
    if not sizes:
        sys.exit(f"Ollama is not reachable at {cfg['local']['ollama_url']} (run `ollama serve`).")
    configured = {m["name"]: m for m in cfg["local"].get("models") or []}
    names = [n.strip() for n in args.models.split(",")] if args.models else list(configured)
    try:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        ram = None
    gb = lambda b: f"{b / 2**30:.1f} GB"
    print(f"RAM: {gb(ram) if ram else 'unknown'}\n")
    rows = []
    for name in names:
        model = configured.get(name, {"name": name, "think": "low" if "gpt-oss" in name else None})
        if model.get("think") is None:
            model.pop("think", None)
        if name not in sizes:
            print(f"{name}: not installed -> ollama pull {name}\n")
            continue
        warn = "  !! uses over 60% of RAM: expect swapping with a browser and IDE open" \
            if ram and sizes[name] > 0.6 * ram else ""
        print(f"{name} ({gb(sizes[name])} on disk){warn}")
        tests = [
            ("router", CLASSIFIER_SYSTEM, "Fix the failing login test in src/auth.py and add a test for emails with spaces", True),
            ("summarize", "Summarize in 3 bullet points.", BENCH_TEXT, False),
            ("commit msg", "Write a conventional commit message for this diff. Output only the message.", BENCH_DIFF, False),
        ]
        r = {"model": name, "size": sizes[name]}
        for label, system, user, as_json in tests:
            try:
                out = local.chat(model, [{"role": "system", "content": system},
                                         {"role": "user", "content": user}], json_mode=as_json, timeout=600)
            except Exception as e:
                print(f"  {label:<11} FAILED: {e}")
                continue
            extra = ""
            if as_json:
                try:
                    cat = json.loads(out["text"]).get("category")
                    extra = f"  -> category={cat} ({'ok' if cat in ('bugfix', 'test') else 'unexpected'})"
                except ValueError:
                    extra = "  -> INVALID JSON"
                r["router_sec"] = out["duration_ms"] / 1000
            if out["load_sec"] > 1:
                extra += f"  (first call loaded the model in {out['load_sec']:.0f}s)"
            print(f"  {label:<11} {out['duration_ms'] / 1000:6.1f}s  prompt {out['prompt_tps'] or '?':>6} tok/s"
                  f"  generate {out['gen_tps'] or '?':>5} tok/s{extra}")
            r.setdefault("gen", []).append(out["gen_tps"] or 0)
        rows.append(r)
        print()
    for r in rows:
        gen = min(r.get("gen") or [0])
        verdict = ("good for simple tasks" if gen >= 10 else
                   "usable but slow; keep it to short answers" if gen >= 5 else
                   "too slow on this machine; pick a smaller or MoE model")
        router = r.get("router_sec")
        tip = "" if router is None or router < 15 else \
            "; routing takes too long, set router.use_llm_classifier=false (keyword routing)"
        print(f"{r['model']}: {verdict}{tip}")


def main():
    ap = argparse.ArgumentParser(prog="otterraft", description="OtterRaft: your AI agents hold hands, so no task drifts away.")
    ap.add_argument("--config", default=os.environ.get("OTTERRAFT_CONFIG", "otterraft.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="write an example config")
    sub.add_parser("serve", help="run scheduler + dashboard")
    a = sub.add_parser("add", help="queue a task ('-' reads the prompt from stdin)")
    a.add_argument("prompt")
    a.add_argument("-w", "--workdir")
    a.add_argument("-t", "--title")
    a.add_argument("-a", "--agent", choices=["auto", "claude", "quick", "local"], default="auto")
    a.add_argument("-v", "--verify", help="shell command that must pass, e.g. 'pytest -q'")
    sub.add_parser("accounts", help="show accounts and cooldowns")
    l = sub.add_parser("login", help="log an account in (opens Claude Code with its config dir)")
    l.add_argument("account")
    sub.add_parser("doctor", help="check CLI, logins and local models")
    b = sub.add_parser("bench", help="measure local models on this machine")
    b.add_argument("-m", "--models", help="comma-separated Ollama tags (default: the configured ones)")
    args = ap.parse_args()
    cfg = config.load(args.config) if args.cmd != "init" else None
    {"init": cmd_init, "serve": cmd_serve, "add": cmd_add, "accounts": cmd_accounts,
     "login": cmd_login, "doctor": cmd_doctor, "bench": cmd_bench}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
