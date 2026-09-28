"""Local LLMs via Ollama (default) or any OpenAI-compatible server (LM Studio, llama.cpp, vLLM)."""
import json
import time
import urllib.error
import urllib.request


class LocalLLM:
    def __init__(self, cfg):
        self.cfg = cfg["local"]

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled") and self.cfg.get("models"))

    def model_for(self, role):
        for m in self.cfg.get("models") or []:
            if role in (m.get("roles") or []):
                return m
        return None

    def available_models(self):
        """Names of models the local server actually has (empty when it is down)."""
        try:
            data = self._get(self.cfg["ollama_url"].rstrip("/") + "/api/tags", timeout=3)
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            try:
                data = self._get(self.cfg["ollama_url"].rstrip("/") + "/v1/models", timeout=3)
                return [m["id"] for m in data.get("data", [])]
            except Exception:
                return []

    def chat(self, model, messages, json_mode=False, timeout=None):
        """Returns dict(text, input_tokens, output_tokens, duration_ms, model)."""
        timeout = timeout or self.cfg.get("timeout_sec", 300)
        base = (model.get("url") or self.cfg["ollama_url"]).rstrip("/")
        t0 = time.time()
        if model.get("provider", "ollama") == "openai":
            body = {"model": model["name"], "messages": messages, "temperature": 0.2}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            data = self._post(base + "/v1/chat/completions", body, timeout, model.get("api_key"))
            u = data.get("usage") or {}
            return {"text": data["choices"][0]["message"]["content"], "model": model["name"],
                    "input_tokens": u.get("prompt_tokens", 0),
                    "output_tokens": u.get("completion_tokens", 0),
                    "duration_ms": int((time.time() - t0) * 1000)}
        body = {"model": model["name"], "messages": messages, "stream": False,
                "options": {"temperature": 0.2, **(model.get("options") or {})},
                "keep_alive": model.get("keep_alive", "30m")}
        if "think" in model:
            body["think"] = model["think"]
        if json_mode:
            body["format"] = "json"
        data = self._post(base + "/api/chat", body, timeout)
        ns = 1e9
        return {"text": (data.get("message") or {}).get("content", ""), "model": model["name"],
                "input_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
                "duration_ms": int((time.time() - t0) * 1000),
                # Ollama's own timings, used by `otterraft bench`
                "load_sec": (data.get("load_duration") or 0) / ns,
                "prompt_tps": _rate(data.get("prompt_eval_count"), data.get("prompt_eval_duration")),
                "gen_tps": _rate(data.get("eval_count"), data.get("eval_duration"))}

    def chat_json(self, role, system, user):
        model = self.model_for(role)
        if not self.enabled or not model:
            return None
        try:
            out = self.chat(model, [{"role": "system", "content": system},
                                    {"role": "user", "content": user}], json_mode=True, timeout=120)
            return json.loads(out["text"])
        except Exception:
            return None

    def model_sizes(self):
        """{name: bytes on disk} for installed Ollama models."""
        try:
            data = self._get(self.cfg["ollama_url"].rstrip("/") + "/api/tags", timeout=3)
            return {m["name"]: m.get("size", 0) for m in data.get("models", [])}
        except Exception:
            return {}

    @staticmethod
    def _get(url, timeout):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())

    @staticmethod
    def _post(url, body, timeout, api_key=None):
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())


def _rate(count, duration_ns):
    return round(count / (duration_ns / 1e9), 1) if count and duration_ns else None
