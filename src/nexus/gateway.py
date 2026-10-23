"""AI 网关 —— OpenAI 兼容门面。

它不是一个"新服务"，而是一层**路由决策**：把模型名解析成后端、把后端不可用降级成可解释的错误。
所有上游状态（注册表、显存预算、后端健康）都来自同一个真相源，避免出现第二份配置。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import audit, config
from .broker import Broker, gpu_state
from .registry import Registry, current_registry


class Gateway:
    def __init__(self, registry: Registry | None = None) -> None:
        self._registry = registry
        self.broker = Broker(self._registry)
        self._lock = threading.Lock()
        self.calls: list[dict] = []

    @property
    def registry(self) -> Registry:
        """按需解析注册表 —— 配置改了不必重启网关。"""
        return self._registry if self._registry is not None else current_registry()

    # -- 路由 ---------------------------------------------------------
    def resolve_route(self, model: str) -> dict:
        entry = self.registry.resolve(model)
        if entry is None:
            return {"ok": False, "reason": f"未知模型 {model}，且注册表为空",
                    "fallback": None}
        backend = entry.backend
        health = self.broker.backend_health(backend)
        verdict = self.broker.fit(entry)
        if not health.get("ok"):
            # 降级链
            for alt in entry.fallback:
                e2 = self.registry.get(alt)
                if e2 is None:
                    continue
                h2 = self.broker.backend_health(e2.backend)
                if h2.get("ok"):
                    return {"ok": True, "model": e2.name, "backend": e2.backend,
                            "url": self.registry.backend_url(e2.backend),
                            "degraded_from": entry.name, "reason": health.get("reason"),
                            "fit": verdict}
            return {"ok": False, "reason": f"后端 {backend} 不可用: {health.get('reason')}",
                    "model": entry.name, "backend": backend}
        return {"ok": True, "model": entry.name, "backend": backend,
                "url": self.registry.backend_url(backend), "fit": verdict}

    # -- 上游调用 ------------------------------------------------------
    def chat_completions(self, payload: dict) -> tuple[int, dict]:
        model = payload.get("model", "")
        route = self.resolve_route(model)
        if not route.get("ok"):
            return 503, {"error": {"message": route.get("reason", "无可用后端"),
                                    "type": "nexus_route_error", "route": route}}
        url = route["url"].rstrip("/")
        if route["backend"] == "ollama":
            upstream = self._ollama_chat(url, payload)
        else:
            upstream = self._openai_passthrough(url, payload)
        code, data = upstream
        with self._lock:
            self.calls.append({"ts": time.time(), "model": route["model"],
                               "backend": route["backend"], "code": code})
            self.calls = self.calls[-200:]
        audit.record("gateway.call", model=route["model"], backend=route["backend"],
                     code=code, degraded=route.get("degraded_from"))
        return code, data

    def _ollama_chat(self, url: str, payload: dict) -> tuple[int, dict]:
        """Ollama /api/chat → OpenAI 响应形状。"""
        messages = payload.get("messages", [])
        tools = payload.get("tools")
        body: dict[str, Any] = {
            "model": payload.get("model"),
            "messages": messages,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        opts = {}
        for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("max_tokens", "num_predict")):
            if payload.get(src) is not None:
                opts[dst] = payload[src]
        if opts:
            body["options"] = opts
        req = urllib.request.Request(f"{url}/api/chat", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1800) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, {"error": {"message": e.read().decode("utf-8", "replace")[:400]}}
            except Exception:
                return e.code, {"error": {"message": f"HTTP {e.code}"}}
        except Exception as exc:
            return 502, {"error": {"message": str(exc)}}
        msg = raw.get("message", {}) or {}
        out: dict[str, Any] = {
            "id": f"nexus-{int(time.time()*1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model"),
            "choices": [{
                "index": 0,
                "message": {"role": msg.get("role", "assistant"),
                            "content": msg.get("content", "")},
                "finish_reason": "stop" if raw.get("done", True) else "length",
            }],
            "usage": {
                "prompt_tokens": raw.get("prompt_eval_count", 0),
                "completion_tokens": raw.get("eval_count", 0),
                "total_tokens": raw.get("prompt_eval_count", 0) + raw.get("eval_count", 0),
            },
            "nexus": {"backend": "ollama", "load_ms": raw.get("load_duration"),
                      "total_ms": raw.get("total_duration")},
        }
        if msg.get("tool_calls"):
            out["choices"][0]["message"]["tool_calls"] = [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": tc.get("function", {}).get("name"),
                              "arguments": json.dumps(tc.get("function", {}).get("arguments", {}),
                                                      ensure_ascii=False)}}
                for i, tc in enumerate(msg["tool_calls"])
            ]
        return 200, out

    def _openai_passthrough(self, url: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(f"{url}/v1/chat/completions",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1800) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, {"error": {"message": e.read().decode("utf-8", "replace")[:400]}}
            except Exception:
                return e.code, {"error": {"message": f"HTTP {e.code}"}}
        except Exception as exc:
            return 502, {"error": {"message": str(exc)}}

    def list_models(self) -> dict:
        out = []
        for m in self.registry.models:
            v = self.broker.fit(m)
            out.append({
                "id": m.name,
                "object": "model",
                "owned_by": f"nexus/{m.backend}",
                "created": int(time.time()),
                "nexus": {"backend": m.backend, "vram_mb": m.vram_mb,
                          "params": m.params, "quant": m.quant,
                          "fit": v["fit"], "fit_reason": v["reason"],
                          "tags": m.tags},
            })
        return {"object": "list", "data": out}


def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NexusGateway/0.2"

        def log_message(self, fmt, *args):  # 静默默认 stderr 日志
            pass

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/healthz", "/health"):
                gpu = gpu_state()
                self._send(200, {"status": "ok", "ts": time.time(),
                                 "gpu": gpu.to_dict(),
                                 "backends": [gateway.broker.backend_health(b)
                                              for b in gateway.registry.backends]})
            elif path == "/v1/models":
                self._send(200, gateway.list_models())
            elif path == "/nexus/route":
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                self._send(200, gateway.resolve_route((q.get("model") or [""])[0]))
            elif path == "/nexus/stats":
                self._send(200, {"calls": gateway.calls[-50:], "count": len(gateway.calls)})
            else:
                self._send(404, {"error": {"message": f"未知路径 {path}"}})

        def do_POST(self):
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._send(400, {"error": {"message": "请求体不是合法 JSON"}})
                return
            if path in ("/v1/chat/completions", "/chat/completions"):
                code, data = gateway.chat_completions(payload)
                self._send(code, data)
            elif path in ("/v1/completions", "/completions"):
                payload.setdefault("messages", [{"role": "user", "content": payload.get("prompt", "")}])
                code, data = gateway.chat_completions(payload)
                self._send(code, data)
            else:
                self._send(404, {"error": {"message": f"未知路径 {path}"}})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    gw = Gateway()
    httpd = ThreadingHTTPServer((host, port), make_handler(gw))
    audit.record("gateway.start", host=host, port=port)
    httpd.serve_forever()
