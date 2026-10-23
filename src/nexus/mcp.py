"""MCP server —— agent 与系统之间的唯一通道。

实现 MCP（Model Context Protocol）over stdio 的最小可用子集，零第三方依赖：
  JSON-RPC 2.0 · initialize / notifications/initialized / tools/list / tools/call / ping

工具按命名空间分组：fs · shell · git · sysd · gpu · journal · nexus
每个工具都受策略层约束（路径根、命令黑名单），与 systemd 沙箱构成双层边界。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable

from . import audit, config, permissions

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "nexus-mcp", "version": "0.2.0"}


# ---------------------------------------------------------------- 策略


class Policy:
    """工具层策略。

    档位（tier）由 permissions 模块统一决定可读根/可写根/命令黑名单；
    policy.toml 里显式写的字段优先于档位默认值。
    """

    def __init__(self, data: dict | None = None, tier: str | None = None) -> None:
        raw = data if data else config.load_layered("policy.toml", {})
        pol = permissions.policy_for(tier, raw)
        self.tier: str = pol["tier"]
        self.read_roots: list[str] = pol["read_roots"]
        self.write_roots: list[str] = pol["write_roots"]
        self.denied_commands: list[str] = pol["denied_commands"]
        self.allow_shell: bool = pol["allow_shell"]
        self.allow_restart: bool = pol["allow_restart"]

    def can_read(self, path: str) -> bool:
        return self._under(path, self.read_roots)

    def can_write(self, path: str) -> bool:
        return self._under(path, self.write_roots)

    def _under(self, path: str, roots: list[str]) -> bool:
        try:
            rp = pathlib.Path(path).expanduser().resolve()
        except Exception:
            return False
        for r in roots:
            try:
                rr = pathlib.Path(r).expanduser().resolve()
            except Exception:
                continue
            if rp == rr or rr in rp.parents:
                return True
        return False

    def command_allowed(self, cmd: str) -> tuple[bool, str]:
        for pat in self.denied_commands:
            if re.search(pat, cmd):
                return False, f"命令被策略拒绝（匹配 {pat}）"
        return True, ""


# ---------------------------------------------------------------- 工具注册


class ToolError(Exception):
    pass


class ToolRegistry:
    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or Policy()
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable[[dict], Any]] = {}
        self._register_builtin()

    # -- 注册 ----------------------------------------------------------
    def register(self, name: str, description: str, schema: dict,
                 fn: Callable[[dict], Any]) -> None:
        self._tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": schema,
        }
        self._handlers[name] = fn

    def list_tools(self) -> list[dict]:
        return list(self._tools.values())

    def call(self, name: str, args: dict | None = None) -> dict:
        args = args or {}
        if name not in self._handlers:
            raise ToolError(f"未知工具 {name}")
        audit.record("mcp.call", tool=name, args=_redact(args))
        try:
            result = self._handlers[name](args)
            return {"ok": True, "tool": name, "result": result}
        except ToolError as exc:
            audit.record("mcp.error", tool=name, error=str(exc))
            return {"ok": False, "tool": name, "error": str(exc)}
        except Exception as exc:
            audit.record("mcp.error", tool=name, error=str(exc))
            return {"ok": False, "tool": name, "error": f"{type(exc).__name__}: {exc}"}

    # -- 内置工具 ------------------------------------------------------
    def _register_builtin(self) -> None:
        R = self.register

        # ---- fs ----
        def _p(args, key="path"):
            p = args.get(key)
            if not p:
                raise ToolError(f"缺少参数 {key}")
            return pathlib.Path(str(p)).expanduser()

        def fs_read(args):
            p = _p(args)
            if not self.policy.can_read(str(p)):
                raise ToolError(f"策略拒绝读取 {p}")
            if p.is_dir():
                raise ToolError(f"{p} 是目录")
            data = p.read_bytes()[: int(args.get("max_bytes", 65536))]
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return {"path": str(p), "binary": True, "bytes": len(data),
                        "preview": repr(data[:64])}
            return {"path": str(p), "bytes": len(data), "content": text}

        def fs_write(args):
            p = _p(args)
            if not self.policy.can_write(str(p)):
                raise ToolError(f"策略拒绝写入 {p}（档位 {self.policy.tier}，"
                                f"可写根: {self.policy.write_roots}）")
            p.parent.mkdir(parents=True, exist_ok=True)
            content = args.get("content", "")
            mode = args.get("mode", "w")
            if mode == "a":
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(str(content))
            else:
                p.write_text(str(content), encoding="utf-8", newline="\n")
            return {"path": str(p), "bytes": p.stat().st_size}

        def fs_list(args):
            p = _p(args)
            if not self.policy.can_read(str(p)):
                raise ToolError(f"策略拒绝读取 {p}")
            if not p.exists():
                raise ToolError(f"路径不存在: {p}")
            items = []
            for child in sorted(p.iterdir())[: int(args.get("limit", 200))]:
                try:
                    st = child.stat()
                    items.append({"name": child.name, "dir": child.is_dir(),
                                  "size": st.st_size})
                except OSError:
                    items.append({"name": child.name, "dir": child.is_dir()})
            return {"path": str(p), "count": len(items), "items": items}

        def fs_stat(args):
            p = _p(args)
            if not self.policy.can_read(str(p)):
                raise ToolError(f"策略拒绝读取 {p}")
            if not p.exists():
                raise ToolError(f"路径不存在: {p}")
            st = p.stat()
            return {"path": str(p), "size": st.st_size, "dir": p.is_dir(),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}

        def fs_mkdir(args):
            p = _p(args)
            if not self.policy.can_write(str(p)):
                raise ToolError(f"策略拒绝创建 {p}")
            p.mkdir(parents=True, exist_ok=True)
            return {"path": str(p), "created": True}

        R("fs.read", "读取文件内容（受可读根约束）",
          {"type": "object", "properties": {"path": {"type": "string"},
                                            "max_bytes": {"type": "integer"}},
           "required": ["path"]}, fs_read)
        R("fs.write", "写入文件（受可写根约束，默认拒绝系统目录）",
          {"type": "object", "properties": {"path": {"type": "string"},
                                            "content": {"type": "string"},
                                            "mode": {"type": "string", "enum": ["w", "a"]}},
           "required": ["path", "content"]}, fs_write)
        R("fs.list", "列出目录内容",
          {"type": "object", "properties": {"path": {"type": "string"},
                                            "limit": {"type": "integer"}},
           "required": ["path"]}, fs_list)
        R("fs.stat", "查看文件属性",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
          fs_stat)
        R("fs.mkdir", "创建目录（受可写根约束）",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
          fs_mkdir)

        # ---- shell ----
        def shell_run(args):
            if not self.policy.allow_shell:
                raise ToolError("策略禁用 shell 工具")
            cmd = args.get("command", "")
            if not cmd:
                raise ToolError("缺少参数 command")
            allowed, why = self.policy.command_allowed(cmd)
            if not allowed:
                raise ToolError(why)
            rc, out, err = config.run(["/bin/bash", "-lc", cmd], timeout=int(args.get("timeout", 120)))
            return {"command": cmd, "rc": rc,
                    "stdout": out[-8000:], "stderr": err[-4000:]}

        def shell_which(args):
            name = args.get("name", "")
            p = shutil.which(name)
            return {"name": name, "path": p, "found": bool(p)}

        R("shell.run", "在 bash 中执行一条命令（策略黑名单过滤）",
          {"type": "object", "properties": {"command": {"type": "string"},
                                            "timeout": {"type": "integer"}},
           "required": ["command"]}, shell_run)
        R("shell.which", "查找可执行文件路径",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
          shell_which)

        # ---- git ----
        def _git(args, *extra):
            repo = args.get("repo", ".")
            cmd = ["git", "-C", str(repo)] + list(extra)
            rc, out, err = config.run(cmd, timeout=30)
            return {"rc": rc, "stdout": out[-8000:], "stderr": err[-2000:]}

        R("git.status", "查看 git 工作区状态",
          {"type": "object", "properties": {"repo": {"type": "string"}}},
          lambda a: _git(a, "status", "--short", "--branch"))
        R("git.log", "查看 git 提交历史",
          {"type": "object", "properties": {"repo": {"type": "string"},
                                            "n": {"type": "integer"}}},
          lambda a: _git(a, "log", f"-{int(a.get('n', 10))}", "--oneline", "--decorate"))
        R("git.diff", "查看 git 未提交改动",
          {"type": "object", "properties": {"repo": {"type": "string"},
                                            "stat": {"type": "boolean"}}},
          lambda a: _git(a, "diff", "--stat" if a.get("stat", True) else "--"))
        R("git.branch", "列出 git 分支",
          {"type": "object", "properties": {"repo": {"type": "string"}}},
          lambda a: _git(a, "branch", "-vv"))

        # ---- sysd ----
        def sysd_list(args):
            rc, out, err = config.run(["systemctl", "--user", "list-units",
                                       "--type=service", "--all", "--no-pager",
                                       "--no-legend"], timeout=20)
            units = []
            for line in out.splitlines():
                parts = line.split(None, 4)
                if len(parts) >= 4:
                    units.append({"unit": parts[0], "load": parts[1],
                                  "active": parts[2], "sub": parts[3],
                                  "desc": parts[4] if len(parts) > 4 else ""})
            return {"units": units, "rc": rc}

        def sysd_show(args):
            unit = args.get("unit", "")
            if not unit:
                raise ToolError("缺少参数 unit")
            rc, out, err = config.run(["systemctl", "--user", "show", unit,
                                       "--no-pager"], timeout=20)
            props = {}
            for line in out.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v
            return {"unit": unit, "properties": props, "rc": rc}

        def sysd_status(args):
            unit = args.get("unit", "")
            rc, out, err = config.run(["systemctl", "--user", "status", unit,
                                       "--no-pager"], timeout=20)
            return {"unit": unit, "rc": rc, "output": out[-6000:], "stderr": err[-2000:]}

        def sysd_restart(args):
            if not self.policy.allow_restart:
                raise ToolError("策略禁用 restart")
            unit = args.get("unit", "")
            if not unit:
                raise ToolError("缺少参数 unit")
            rc, out, err = config.run(["systemctl", "--user", "restart", unit], timeout=60)
            return {"unit": unit, "rc": rc, "stdout": out, "stderr": err}

        def sysd_logs(args):
            unit = args.get("unit", "")
            n = int(args.get("n", 50))
            cmd = ["journalctl", "--user"]
            if unit:
                cmd += ["-u", unit]
            cmd += ["-n", str(n), "--no-pager"]
            rc, out, err = config.run(cmd, timeout=20)
            return {"unit": unit, "rc": rc, "output": out[-8000:]}

        R("sysd.list", "列出所有 systemd --user 服务单元",
          {"type": "object", "properties": {}}, sysd_list)
        R("sysd.show", "查看单元全部属性（systemctl show）",
          {"type": "object", "properties": {"unit": {"type": "string"}}, "required": ["unit"]},
          sysd_show)
        R("sysd.status", "查看单元状态",
          {"type": "object", "properties": {"unit": {"type": "string"}}, "required": ["unit"]},
          sysd_status)
        R("sysd.restart", "重启单元（受策略约束）",
          {"type": "object", "properties": {"unit": {"type": "string"}}, "required": ["unit"]},
          sysd_restart)
        R("sysd.logs", "读取单元日志",
          {"type": "object", "properties": {"unit": {"type": "string"},
                                            "n": {"type": "integer"}}}, sysd_logs)

        # ---- gpu ----
        from .broker import Broker, gpu_state

        def gpu_snapshot(_a):
            g = gpu_state()
            b = Broker()
            return {"gpu": g.to_dict(), "resident": b.residency(),
                    "budget_mb": max(0, g.total_mb - b.registry.reserve_mb),
                    "reserve_mb": b.registry.reserve_mb}

        def gpu_models(_a):
            b = Broker()
            g = gpu_state()
            out = []
            for m in b.registry.models:
                v = b.fit(m, g)
                out.append({**m.to_dict(), "fit": v["fit"], "reason": v["reason"]})
            return {"models": out, "gpu": g.to_dict()}

        def gpu_load(args):
            name = args.get("name", "")
            b = Broker()
            return b.load(name, force=bool(args.get("force")))

        def gpu_unload(args):
            name = args.get("name", "")
            b = Broker()
            return b.unload(name)

        R("gpu.snapshot", "采集显存快照与驻留模型",
          {"type": "object", "properties": {}}, gpu_snapshot)
        R("gpu.models", "列出注册表模型与可容纳性判定",
          {"type": "object", "properties": {}}, gpu_models)
        R("gpu.load", "加载模型（独占语义 + 显存预算）",
          {"type": "object", "properties": {"name": {"type": "string"},
                                            "force": {"type": "boolean"}},
           "required": ["name"]}, gpu_load)
        R("gpu.unload", "卸载模型释放显存",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
          gpu_unload)

        # ---- journal ----
        def journal_query(args):
            n = int(args.get("n", 50))
            prio = args.get("priority")
            cmd = ["journalctl", "--user", "-n", str(n), "--no-pager", "-o", "short-iso"]
            if prio:
                cmd += ["-p", str(prio)]
            rc, out, err = config.run(cmd, timeout=30)
            return {"rc": rc, "output": out[-10000:]}

        def journal_tail(args):
            n = int(args.get("n", 30))
            rc, out, err = config.run(["journalctl", "--user", "-n", str(n),
                                       "--no-pager"], timeout=20)
            return {"rc": rc, "output": out[-8000:]}

        def journal_errors(args):
            since = args.get("since", "-1h")
            rc, out, err = config.run(["journalctl", "--user", "--since", since,
                                       "-p", "err", "--no-pager"], timeout=30)
            return {"rc": rc, "output": out[-10000:]}

        R("journal.query", "按条件查询 journal",
          {"type": "object", "properties": {"n": {"type": "integer"},
                                            "priority": {"type": "string"}}}, journal_query)
        R("journal.tail", "读取最近 journal 条目",
          {"type": "object", "properties": {"n": {"type": "integer"}}}, journal_tail)
        R("journal.errors", "读取错误级别日志",
          {"type": "object", "properties": {"since": {"type": "string"}}}, journal_errors)

        # ---- nexus ----
        def nexus_registry(_a):
            from .registry import current_registry
            return current_registry().summary()

        def nexus_incidents(args):
            d = config.incidents_dir()
            if not d.exists():
                return {"incidents": []}
            items = []
            for p in sorted(d.rglob("*.json"), reverse=True)[:20]:
                items.append({"path": str(p), "mtime": p.stat().st_mtime})
            return {"incidents": items, "dir": str(d)}

        def nexus_audit(args):
            from . import audit as _a
            return {"entries": _a.read_recent(int(args.get("n", 20)))}

        R("nexus.registry", "查看模型注册表摘要",
          {"type": "object", "properties": {}}, nexus_registry)
        R("nexus.incident", "列出已归档的事件诊断",
          {"type": "object", "properties": {}}, nexus_incidents)
        R("nexus.audit", "读取最近审计记录",
          {"type": "object", "properties": {"n": {"type": "integer"}}}, nexus_audit)


def _redact(args: dict) -> dict:
    """审计里对敏感键做遮蔽。"""
    secret = {"token", "password", "secret", "api_key", "authorization"}
    return {k: ("***" if k.lower() in secret else v) for k, v in (args or {}).items()}


# ---------------------------------------------------------------- MCP 协议


class MCPServer:
    """stdio 上的 MCP server。"""

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        elif method == "notifications/initialized":
            return None  # 通知，不回响应
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.registry.list_tools()}
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            r = self.registry.call(name, args)
            text = json.dumps(r.get("result", r), ensure_ascii=False)
            result = {"content": [{"type": "text", "text": text}],
                      "isError": not r.get("ok", True)}
        elif method in ("resources/list", "prompts/list"):
            result = {"resources": [], "prompts": []}
        else:
            if mid is None:
                return None
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"未知方法 {method}"}}

        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = self.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                stdout.flush()


class MCPClient:
    """进程内客户端 —— agent 直接调用，不走 stdio 往返。"""

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()

    def tools_for_llm(self) -> list[dict]:
        """转成 Ollama / OpenAI 的 function-calling 形状。"""
        out = []
        for t in self.registry.list_tools():
            out.append({"type": "function",
                        "function": {"name": t["name"],
                                     "description": t["description"],
                                     "parameters": t["inputSchema"]}})
        return out

    def call(self, name: str, args: dict | None = None) -> dict:
        return self.registry.call(name, args or {})


def main() -> None:
    """`nexus-mcp` 入口：作为 stdio MCP server 运行。"""
    server = MCPServer()
    audit.record("mcp.server.start", pid=os.getpid())
    server.serve()


if __name__ == "__main__":
    main()
