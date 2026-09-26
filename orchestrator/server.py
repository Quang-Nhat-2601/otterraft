"""HTTP API + Server-Sent Events + static dashboard. Standard library only."""
import hmac
import json
import queue
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config

STATIC = Path(__file__).parent / "static"


def stats(orch):
    db, now = orch.db, time.time()
    accounts = []
    for a in orch.cfg["accounts"]:
        st = orch.pool.state(a["name"])
        agg = lambda since: db.one(
            "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(input_tokens),0) inp, "
            "COALESCE(SUM(output_tokens),0) outp, COALESCE(AVG(ok),0) ok, COALESCE(AVG(duration_ms),0) dur "
            "FROM usage WHERE kind='claude' AND name=? AND ts>?", (a["name"], since))
        accounts.append({
            "name": a["name"], "enabled": a.get("enabled", True), "priority": a.get("priority", 9),
            "config_dir": a.get("config_dir") or "~/.claude",
            "running": orch.pool.running.get(a["name"], 0), "max_parallel": a.get("max_parallel", 1),
            "cooldown_until": st["cooldown_until"] if st["cooldown_until"] > now else 0,
            "last_error": st.get("last_error"),
            "limits": st.get("limits"), "limits_at": st.get("limits_at"),
            "utilization_5h": orch.pool.utilization(a["name"]),
            "window_5h": agg(now - 5 * 3600), "week": agg(now - 7 * 86400), "all": agg(0)})
    locals_ = db.query(
        "SELECT name, COUNT(*) n, COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) outp, "
        "COALESCE(AVG(ok),0) ok, COALESCE(AVG(duration_ms),0) dur FROM usage WHERE kind='local' GROUP BY name")
    installed = orch.local.available_models() if orch.local.enabled else []
    for m in orch.cfg["local"].get("models") or []:
        if not any(l["name"] == m["name"] for l in locals_):
            locals_.append({"name": m["name"], "n": 0, "inp": 0, "outp": 0, "ok": 0, "dur": 0})
    for l in locals_:
        l["installed"] = l["name"] in installed
    by_cat = db.query(
        "SELECT category, kind, COUNT(*) n, AVG(ok) ok FROM usage GROUP BY category, kind ORDER BY n DESC")
    daily = db.query(
        "SELECT date(ts,'unixepoch','localtime') day, kind, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost, "
        "COALESCE(SUM(input_tokens+output_tokens),0) tokens FROM usage WHERE ts>? GROUP BY day, kind ORDER BY day",
        (now - 14 * 86400,))
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    saved = db.one("SELECT COUNT(*) n, COALESCE(SUM(input_tokens+output_tokens),0) tokens FROM usage "
                   "WHERE kind='local' AND ok=1")
    return {"accounts": accounts, "local": locals_, "local_server_up": bool(installed),
            "by_category": by_cat, "daily": daily, "counts": counts, "local_saved": saved,
            "config": {"permission_mode": orch.cfg["claude"]["permission_mode"],
                       "auto_accept": orch.cfg.get("auto_accept")}}


class Handler(BaseHTTPRequestHandler):
    orch = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- plumbing ----------------------------------------------------------------
    def _authed(self):
        token = self.orch.cfg["auth_token"]
        q = parse_qs(urlparse(self.path).query)
        given = self.headers.get("Authorization", "").removeprefix("Bearer ") or q.get("token", [""])[0]
        return hmac.compare_digest(given.encode(), token.encode())

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body, ensure_ascii=False, default=str)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # -- routes ------------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        p, q = url.path, parse_qs(url.query)
        if p in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html")
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        o = self.orch
        if p == "/api/tasks":
            return self._send(200, o.db.tasks())
        if p.startswith("/api/tasks/") and p.endswith("/events"):
            tid = int(p.split("/")[3])
            return self._send(200, o.db.events(tid, int(q.get("after", ["0"])[0])))
        if p.startswith("/api/tasks/"):
            t = o.db.task(int(p.split("/")[3]))
            return self._send(200 if t else 404, t or {"error": "not found"})
        if p == "/api/stats":
            return self._send(200, stats(o))
        if p == "/api/lessons":
            return self._send(200, o.db.query("SELECT * FROM lessons ORDER BY pending DESC, enabled DESC, score DESC, id DESC"))
        if p == "/api/permissions":
            return self._send(200, o.db.query("SELECT * FROM permission_suggestions ORDER BY count DESC"))
        if p == "/api/stream":
            return self._stream()
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, {"error": "Content-Type must be application/json"})
        o, p = self.orch, urlparse(self.path).path
        try:
            b = self._body()
        except ValueError:
            return self._send(400, {"error": "invalid json"})
        parts = p.strip("/").split("/")
        if p == "/api/tasks":
            if not (b.get("prompt") or "").strip():
                return self._send(400, {"error": "prompt required"})
            tid = o.submit(b["prompt"], b.get("title", ""), b.get("workdir", ""),
                           b.get("agent_pref", "auto"), b.get("verify_cmd", ""))
            return self._send(201, {"id": tid})
        if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
            tid, action = int(parts[2]), parts[3]
            if not o.db.task(tid):
                return self._send(404, {"error": "not found"})
            if action == "cancel":
                o.cancel(tid)
            elif action == "reply":
                o.reply(tid, b.get("message", ""))
            elif action == "review":
                o.review(tid, bool(b.get("accepted")), b.get("feedback", ""))
            elif action == "retry":
                o.db.update_task(tid, status="queued", agent=None if b.get("reroute") else o.db.task(tid)["agent"],
                                 agent_pref=b.get("agent_pref") or o.db.task(tid)["agent_pref"],
                                 session_id=None, pending_message=None, error=None, progress=0, todos=None)
                o.emit(tid, "retry", b)
                o.changed(tid)
            else:
                return self._send(404, {"error": "unknown action"})
            return self._send(200, {"ok": True})
        if len(parts) == 4 and parts[:2] == ["api", "accounts"] and parts[3] == "reset":
            o.pool.reset(parts[2])
            return self._send(200, {"ok": True})
        if p == "/api/lessons":
            lid = o.learner.add_lesson(b.get("text", ""), b.get("scope") or "global", b.get("tags") or [])
            return self._send(201, {"id": lid})
        if len(parts) == 3 and parts[:2] == ["api", "lessons"]:
            lid = int(parts[2])
            if b.get("delete"):
                o.db.execute("DELETE FROM lessons WHERE id=?", (lid,))
            elif b.get("approve"):
                o.db.execute("UPDATE lessons SET pending=0, enabled=1 WHERE id=?", (lid,))
            else:
                o.db.execute("UPDATE lessons SET enabled=? WHERE id=?", (1 if b.get("enabled") else 0, lid))
            return self._send(200, {"ok": True})
        if len(parts) == 3 and parts[:2] == ["api", "permissions"]:
            row = o.db.one("SELECT * FROM permission_suggestions WHERE id=?", (int(parts[2]),))
            if not row:
                return self._send(404, {"error": "not found"})
            status = "approved" if b.get("approve") else "dismissed"
            o.db.execute("UPDATE permission_suggestions SET status=? WHERE id=?", (status, row["id"]))
            tools = o.cfg["claude"].setdefault("allowed_tools", [])
            if status == "approved" and row["rule"] not in tools:
                tools.append(row["rule"])  # takes effect for the next Claude run
                config.persist(o.cfg, ["claude", "allowed_tools"], tools)
            return self._send(200, {"ok": True, "allowed_tools": tools})
        self._send(404, {"error": "not found"})

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        sub = self.orch.bus.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = sub.get(timeout=15)
                    data = json.dumps(msg, ensure_ascii=False, default=str)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.orch.bus.unsubscribe(sub)
            self.close_connection = True


def serve(orch):
    Handler.orch = orch
    httpd = ThreadingHTTPServer((orch.cfg["host"], orch.cfg["port"]), Handler)
    httpd.daemon_threads = True
    return httpd
