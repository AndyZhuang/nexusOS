"""Agent 宿主 —— Nexus 不造 agent，只做 agent 的系统集成层。

设计立场（docs/00-DESIGN-REVIEW.md H3）：
Omarchy 的做法是复用现成 agent CLI 并懒加载，其产品宣言是
"Every agent, pre-wired, lazy-loaded"。自研 harness 是几周起步且无社区兜底的时间黑洞。

因此本模块只做五件事：
  ① system prompt / skill 注入      ② 权限策略
  ③ MCP server 供给                 ④ 审计              ⑤ 会话与记忆落盘

适配器：
  local —— Ollama 驱动的工具调用循环（离线唯一可跑路径）
  cli   —— 包装 claude / opencode 等现成 CLI（有外网时接管）
  mock  —— 确定性回放（CI 与事件总线自测）
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import shutil
import subprocess
import time
from typing import Any

from . import audit, config, permissions, sandbox
from .broker import Broker, gpu_state
from .mcp import MCPClient, Policy, ToolRegistry
from .registry import Registry, current_registry


@dataclasses.dataclass
class AgentResult:
    ok: bool
    output: str = ""
    adapter: str = ""
    model: str = ""
    iterations: int = 0
    tool_calls: list[dict] = dataclasses.field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------- 提示词


def system_context() -> str:
    """注入给 agent 的系统事实快照。让 agent 不用猜环境。"""
    reg = current_registry()
    gpu = gpu_state()
    lines = [
        "=== NexusOS 系统事实 ===",
        f"主机: {os.uname().nodename}  内核: {os.uname().release}",
        f"数据目录: {config.data_home()}",
        f"配置目录: {config.config_home()}",
        f"GPU: {gpu.name or '未知'}  总显存 {gpu.total_mb}MB  已用 {gpu.used_mb}MB  空闲 {gpu.free_mb}MB",
        f"GPU 预算: {max(0, gpu.total_mb - reg.reserve_mb)}MB (预留 {reg.reserve_mb}MB)",
    ]
    try:
        res = Broker(reg).residency()
        if res:
            lines.append("已驻留模型: " + ", ".join(f"{r['name']}({r['size_mb']}MB)" for r in res))
        else:
            lines.append("已驻留模型: 无")
    except Exception:
        pass
    lines.append("可用模型: " + ", ".join(
        f"{m.name}[{m.params or '?'} {m.quant or '?'} ~{m.vram_mb}MB]"
        for m in reg.models) or "（注册表为空）")
    lines.append("=== 工具说明 ===")
    lines.append("你可以通过 MCP 工具直接操作这台机器。工具名前缀含义："
                 "fs.(文件) shell.(命令) git.(版本) sysd.(systemd 单元) "
                 "gpu.(显存/模型) journal.(日志) nexus.(系统元数据)")
    lines.append("规则：先调用工具获取事实，再给出结论；不要凭空猜测系统状态。")
    return "\n".join(lines)


def build_system_prompt(tools: list[dict] | None = None,
                        native_tools: bool = True) -> str:
    """读取 prompts/ 下的片段并拼装。

    native_tools=False 时，把工具定义以文本形式注入 —— 小模型不支持
    原生 function-calling 时的回退路径。
    """
    parts = []
    pdir = config.config_home() / "prompts"
    for name in ("system.md", "nexus.md"):
        f = pdir / name
        if f.exists():
            parts.append(f.read_text(encoding="utf-8", errors="replace").strip())
    if tools and not native_tools:
        parts.append(render_tools_prompt(tools))
    parts.append(system_context())
    return "\n\n".join(p for p in parts if p)


def render_tools_prompt(tools: list[dict]) -> str:
    """把工具定义渲染成 prompt（原生 tools 不可用时的回退）。"""
    lines = ["# 可用工具",
             "当你需要获取系统事实或改变系统状态时，输出一个工具调用，格式严格如下：",
             "",
             "<tool_call>",
             '{"name": "工具的完整名称", "arguments": {"参数名": "参数值"}}',
             "</tool_call>",
             "",
             "重要规则：",
             "1. name 必须是下面列出的完整工具名，一个字都不能少（例如 gpu.models，不是 gpu）",
             "2. arguments 必须是 JSON 对象；该工具没有参数时写 {}",
             "3. 一次只能调用一个工具，拿到结果后再决定下一步",
             "4. 如果不需要工具，直接用自然语言回答，不要输出 <tool_call> 标签",
             "",
             "工具说明："]
    for t in tools:
        fn = t.get("function", t)
        schema = fn.get("parameters", {}) or {}
        props = schema.get("properties", {}) or {}
        req = set(schema.get("required", []) or [])
        sig = ", ".join(
            f"{k}{'' if k in req else '?'}:{v.get('type','any')}"
            for k, v in props.items())
        arghint = (f"({sig})" if sig else "()")
        lines.append(f"- {fn.get('name')}{arghint} —— {fn.get('description','')}")
    return "\n".join(lines)


# 命名空间别名：模型经常只写命名空间（"gpu"），这里补全到该命名空间的主工具
NS_ALIASES = {
    "fs": "fs.list", "file": "fs.list", "files": "fs.list",
    "read": "fs.read", "write": "fs.write",
    "shell": "shell.run", "bash": "shell.run", "exec": "shell.run", "cmd": "shell.run",
    "git": "git.status",
    "sysd": "sysd.list", "systemd": "sysd.list", "systemctl": "sysd.list",
    "service": "sysd.status", "unit": "sysd.status",
    "gpu": "gpu.snapshot", "nvidia": "gpu.snapshot", "vram": "gpu.snapshot",
    "models": "gpu.models", "model": "gpu.models",
    "journal": "journal.query", "log": "journal.query", "logs": "journal.query",
    "nexus": "nexus.registry",
}

_INVISIBLE = dict.fromkeys(map(ord, "‌‍﻿"), None)


def resolve_tool_name(name: str, valid_names: set[str]) -> str | None:
    """把模型给出的（可能不完整的）工具名解析成合法工具名。"""
    if not name:
        return None
    if name in valid_names:
        return name
    low = name.lower()
    if low in valid_names:
        return low
    # 命名空间别名
    if low in NS_ALIASES and NS_ALIASES[low] in valid_names:
        return NS_ALIASES[low]
    # 前缀唯一匹配： "gpu.m" → gpu.models
    cands = [v for v in valid_names if v.lower().startswith(low + ".") or v.lower().startswith(low)]
    cands = [c for c in cands if c.lower() != low]
    if len(cands) == 1:
        return cands[0]
    # 命名空间 + 参数语义： "gpu" + {"models": true} → gpu.models
    return None


def filter_args(tool_name: str, args: dict, tools: list[dict]) -> dict:
    """按工具 schema 过滤参数，丢掉模型幻觉出来的字段。"""
    for t in tools:
        fn = t.get("function", t)
        if fn.get("name") != tool_name:
            continue
        props = (fn.get("parameters", {}) or {}).get("properties", {}) or {}
        if not props:
            return {}
        return {k: v for k, v in args.items() if k in props}
    return args


_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_tool_call(text: str, valid_names: set[str]) -> tuple[str, dict] | None:
    """从模型输出里解析出一个工具调用。找不到或非法返回 None。

    按可靠性从高到低尝试：标准 <tool_call> 标签 → 围栏 JSON → 首个平衡 JSON。
    """
    # 模型输出里常混入零宽字符（U+200B 等），先清洗
    text = (text or "").translate(_INVISIBLE)
    candidates: list[str] = []
    candidates += _TOOL_CALL_RE.findall(text)
    candidates += _FENCE_RE.findall(text)
    # 平衡括号扫描：找一个能解析成 dict 且含 name/tool 的 JSON
    depth, start, instr, esc = 0, None, False, False
    for i, ch in enumerate(text):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start:i + 1])
                    start = None
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
        if not name:
            continue
        args = obj.get("arguments")
        if args is None:
            args = obj.get("parameters") or obj.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"value": args}
        if not isinstance(args, dict):
            args = {}
        resolved = resolve_tool_name(str(name), valid_names)
        if resolved:
            return resolved, args
    return None


# ---------------------------------------------------------------- 适配器


class Adapter:
    name = "base"
    available_flag = False

    def available(self) -> bool:
        return self.available_flag

    def run(self, task: str, *, sandbox_tier: str = "strict",
            max_iterations: int = 6, **kw) -> AgentResult:  # pragma: no cover
        raise NotImplementedError


class LocalAdapter(Adapter):
    """Ollama 驱动的工具调用循环。

    双模式：后端支持原生 function-calling 时直接用；不支持（例如 llama 系模板）
    自动退回自实现的 prompt-based 循环 —— 对任何模型都成立。
    """

    name = "local"

    def __init__(self) -> None:
        self.registry = current_registry()
        self.client = MCPClient(ToolRegistry(Policy()))
        self.url = self.registry.backend_url("ollama")
        self._native_tools: bool | None = None

    def available(self) -> bool:
        if not self.url:
            return False
        import urllib.request
        try:
            with urllib.request.urlopen(f"{self.url.rstrip('/')}/api/tags", timeout=4):
                return True
        except Exception:
            return False

    # -- 能力探测 ------------------------------------------------------
    def native_tools_supported(self, model: str) -> bool:
        """探测后端是否支持原生 tools。探测一次后缓存。"""
        if self._native_tools is not None:
            return self._native_tools
        import urllib.error
        import urllib.request
        body = {"model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function",
                           "function": {"name": "probe", "description": "probe",
                                        "parameters": {"type": "object", "properties": {}}}}],
                "stream": False, "options": {"num_predict": 1}}
        req = urllib.request.Request(f"{self.url.rstrip('/')}/api/chat",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600):
                self._native_tools = True
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            self._native_tools = "does not support tools" not in detail
            if not self._native_tools:
                audit.record("agent.tools_unsupported", model=model, detail=detail[:200])
        except Exception:
            self._native_tools = False
        return self._native_tools

    # -- 主循环 --------------------------------------------------------
    def run(self, task: str, *, sandbox_tier: str = "strict",
            max_iterations: int = 6, model: str | None = None, **kw) -> AgentResult:
        import urllib.error
        import urllib.request

        started = time.time()
        name = model or self.registry.default_model
        entry = self.registry.get(name)
        if entry is None:
            entry = self.registry.resolve("")
        if entry is None:
            return AgentResult(ok=False, adapter=self.name,
                               error="注册表为空，没有可用模型")
        model = entry.name

        # 按本次的档位重建工具策略（可写根随档位变化）
        self.client = MCPClient(ToolRegistry(Policy(tier=sandbox_tier)))

        # 显存仲裁：加载前先问 broker
        verdict = Broker().fit(entry)
        if verdict["fit"] == "cpu":
            return AgentResult(ok=False, adapter=self.name, model=model,
                               error=f"显存不足，无法加载 {model}: {verdict['reason']}")

        tools = self.client.tools_for_llm()
        native = self.native_tools_supported(model)
        messages: list[dict] = [
            {"role": "system", "content": build_system_prompt(tools, native_tools=native)},
            {"role": "user", "content": task},
        ]
        valid_names = {t["function"]["name"] for t in tools}
        calls: list[dict] = []

        for it in range(1, max_iterations + 1):
            body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
            if native:
                body["tools"] = tools
            req = urllib.request.Request(f"{self.url.rstrip('/')}/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=1800) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass
                return AgentResult(ok=False, adapter=self.name, model=model,
                                   iterations=it, error=f"HTTP {e.code}: {detail}",
                                   duration_ms=int((time.time() - started) * 1000))
            except Exception as exc:
                return AgentResult(ok=False, adapter=self.name, model=model,
                                   iterations=it, error=str(exc),
                                   duration_ms=int((time.time() - started) * 1000))

            msg = raw.get("message", {}) or {}
            tcalls = msg.get("tool_calls") or []
            content = msg.get("content", "") or ""

            if not tcalls and not native:
                # 回退模式：从文本里解析工具调用
                parsed = parse_tool_call(content, valid_names)
                if parsed:
                    tname, targs = parsed
                    tcalls = [{"function": {"name": tname, "arguments": targs}}]

            if not tcalls:
                return AgentResult(ok=True, output=content,
                                   adapter=self.name, model=model, iterations=it,
                                   tool_calls=calls,
                                   duration_ms=int((time.time() - started) * 1000))

            messages.append({"role": "assistant", "content": content})
            for tc in tcalls:
                fn = tc.get("function", {}) or {}
                tname = fn.get("name", "")
                targs = fn.get("arguments") or {}
                if isinstance(targs, str):
                    try:
                        targs = json.loads(targs)
                    except Exception:
                        targs = {}
                res = self.client.call(tname, filter_args(tname, targs, tools))
                calls.append({"tool": tname, "args": targs,
                              "ok": res.get("ok"), "result": res.get("result"),
                              "error": res.get("error")})
                messages.append({"role": "tool", "name": tname,
                                 "content": json.dumps(res.get("result", res),
                                                      ensure_ascii=False)})
        return AgentResult(ok=True, output="达到最大迭代次数，未得到最终答复。",
                           adapter=self.name, model=model, iterations=max_iterations,
                           tool_calls=calls,
                           duration_ms=int((time.time() - started) * 1000))


class CliAdapter(Adapter):
    """包装现成 agent CLI（claude / opencode ...）。有外网时接管。"""

    name = "cli"

    def __init__(self, binary: str = "claude", extra_args: list[str] | None = None) -> None:
        self.binary = binary
        self.extra_args = extra_args or []
        self.path = shutil.which(binary)

    def available(self) -> bool:
        return self.path is not None

    def run(self, task: str, *, sandbox_tier: str = "standard", **kw) -> AgentResult:
        if not self.available():
            return AgentResult(ok=False, adapter=self.name,
                               error=f"未找到 {self.binary}（有外网时可安装或用 mise 懒加载）")
        started = time.time()
        prompt = task
        # 已经在沙箱子进程里就别再套一层（外层已经用目标档位起好了）
        inner_tier = "off" if os.environ.get(SANDBOX_CHILD_ENV) else sandbox_tier
        rc, out, err = sandbox.run_sandboxed(
            [self.path] + self.extra_args + ["-p", prompt],
            tier=inner_tier, timeout=900)
        audit.record("agent.cli", binary=self.binary, rc=rc, tier=inner_tier)
        return AgentResult(ok=rc == 0, output=out or err, adapter=self.name,
                           iterations=1, error=None if rc == 0 else f"exit {rc}",
                           duration_ms=int((time.time() - started) * 1000))


class MockAdapter(Adapter):
    """确定性适配器：不调模型，按规则产出可预期的工具调用序列。

    用途：事件总线自测、CI、以及在没有模型时验证"系统能否自动诊断"。
    """

    name = "mock"
    available_flag = True

    def run(self, task: str, *, sandbox_tier: str = "standard", **kw) -> AgentResult:
        started = time.time()
        client = MCPClient(ToolRegistry(Policy(tier=sandbox_tier)))
        seq = [
            ("gpu.snapshot", {}),
            ("sysd.list", {}),
            ("journal.errors", {"since": "-1h"}),
        ]
        calls = []
        for tname, targs in seq:
            res = client.call(tname, targs)
            calls.append({"tool": tname, "args": targs, "ok": res.get("ok"),
                          "result": res.get("result")})
        out = (f"[mock] 已对任务『{task}』采集 {len(calls)} 项系统事实：\n"
               + "\n".join(f"- {c['tool']}: {'ok' if c['ok'] else c.get('error')}"
                           for c in calls)
               + "\n结论：系统可观测性链路正常，事件→诊断路径已打通。")
        return AgentResult(ok=True, output=out, adapter=self.name, iterations=1,
                           tool_calls=calls,
                           duration_ms=int((time.time() - started) * 1000))


ADAPTERS = {"local": LocalAdapter, "mock": MockAdapter}


def get_adapter(name: str) -> Adapter:
    if name == "cli":
        return CliAdapter()
    cls = ADAPTERS.get(name)
    if cls is None:
        raise ValueError(f"未知适配器 {name}（可用: {', '.join(list(ADAPTERS) + ['cli'])}）")
    return cls()


# ---------------------------------------------------------------- 运行入口

# 沙箱子进程标记：设了这个变量就说明已经在沙箱里，不要再套一层
SANDBOX_CHILD_ENV = "NEXUS_SANDBOX_CHILD"


def _child_env() -> dict[str, str]:
    """传给沙箱子进程的环境。必须显式带全，否则 systemd 单元里 PATH 是残的。"""
    path = os.environ.get("PATH", "")
    extra = "/usr/lib/wsl/lib"  # WSL 下 nvidia-smi 在这里，不在 PATH
    if extra not in path:
        path = path + ":" + extra
    env = {
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "NEXUS_DATA_HOME": str(config.data_home()),
        "NEXUS_CONFIG_HOME": str(config.config_home()),
        "HOME": str(pathlib.Path.home()),
        "PATH": path,
        SANDBOX_CHILD_ENV: "1",
    }
    return {k: v for k, v in env.items() if v}


def run_in_sandbox_child(task: str, adapter: str, model: str | None,
                         max_iterations: int, tier: str) -> AgentResult:
    """把整个 agent 进程放进 systemd 沙箱再跑。

    为什么必须这样：v0.2 里 local 适配器的工具调用是在 nexus 主进程内直接执行的，
    sandbox_tier 只是个记录参数 —— 所谓"双层边界"实际只有 MCP 策略层一层。
    把 agent 进程整体塞进 systemd-run 之后，内核级的 ProtectSystem/ProtectHome
    才真正对工具调用生效，档位才有意义。
    """
    started = time.time()
    exe = shutil.which("systemd-run") or "/usr/bin/systemd-run"
    python = shutil.which("python3") or "python3"
    props = sandbox.build_properties(tier)

    argv = [exe, "--user", "--quiet", "--wait", "--pipe", "--collect"]
    for p in props:
        argv += ["-p", p]
    env = _child_env()
    for k, v in env.items():
        argv += ["--setenv", f"{k}={v}"]
    argv += ["--", python, "-m", "nexus.cli", "__agent-child"]

    payload = json.dumps({"task": task, "adapter": adapter, "model": model,
                          "max_iterations": max_iterations}, ensure_ascii=False)
    result_file = config.state_dir() / "agent-result.json"
    try:
        result_file.unlink()
    except OSError:
        pass

    rc, out, err = config.run(argv, timeout=1800, env=env, stdin_data=payload)
    if result_file.exists():
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            return AgentResult(**data)
        except Exception as exc:
            return AgentResult(ok=False, adapter=adapter,
                               error=f"沙箱子进程结果解析失败: {exc}",
                               duration_ms=int((time.time() - started) * 1000))
    tail = (err or out or "").strip()[-1200:]
    return AgentResult(ok=False, adapter=adapter,
                       error=f"沙箱子进程未产出结果 (rc={rc})\n{tail}",
                       duration_ms=int((time.time() - started) * 1000))


def run_agent(task: str, *, adapter: str = "local",
              sandbox_tier: str | None = None,
              model: str | None = None, max_iterations: int = 6,
              save_session: bool = True) -> AgentResult:
    """统一入口：选适配器 → （进沙箱）→ 执行 → 落会话与审计。"""
    tier = sandbox_tier or permissions.resolve_tier()
    if tier not in permissions.TIERS:
        tier = permissions.DEFAULT_TIER

    if tier != "off":
        audit.record("agent.run", task=task[:200], adapter=adapter, tier=tier)
    else:
        audit.record("agent.run.unsafe", task=task[:200], adapter=adapter,
                     note="显式放开沙箱边界")

    in_child = bool(os.environ.get(SANDBOX_CHILD_ENV))
    if tier != "off" and not in_child and sandbox.systemd_run_available():
        # 父进程不进沙箱，改为派生子进程在沙箱里跑真正的 agent 循环
        result = run_in_sandbox_child(task, adapter, model, max_iterations, tier)
        audit.record("agent.result", adapter=adapter, ok=result.ok,
                     iterations=result.iterations,
                     tools=[c["tool"] for c in result.tool_calls],
                     duration_ms=result.duration_ms, error=result.error, tier=tier)
        if save_session:
            save_session_record(task, result)
        return result

    adp = get_adapter(adapter)
    if not adp.available():
        return AgentResult(ok=False, adapter=adapter,
                           error=f"适配器 {adapter} 不可用（local 需要 ollama 后端在线）")
    result = adp.run(task, sandbox_tier=tier, model=model,
                      max_iterations=max_iterations)
    audit.record("agent.result", adapter=adapter, ok=result.ok,
                 iterations=result.iterations, tools=[c["tool"] for c in result.tool_calls],
                 duration_ms=result.duration_ms, error=result.error, tier=tier)
    if save_session:
        save_session_record(task, result)
    return result


def save_session_record(task: str, result: AgentResult) -> None:
    d = config.sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    sid = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    payload = {"id": sid, "ts": time.time(), "task": task, **result.to_dict()}
    (d / f"{sid}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8", newline="\n")
