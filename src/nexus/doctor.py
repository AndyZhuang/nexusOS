"""诊断上下文构建器 —— 给 agent 喂"事实"，而不是让它猜。

Omarchy 的崩溃自诊断之所以有效，是因为它把 core dump 和相关日志**结构化地**交给 agent。
本模块是同一思路的系统化实现：任何一次诊断，都先采集一份可复现的事实快照。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from . import config
from .broker import Broker, gpu_state


def _run(cmd: list[str], timeout: int = 15) -> str:
    rc, out, err = config.run(cmd, timeout=timeout)
    return out.strip() or err.strip()


def system_facts() -> dict:
    facts: dict[str, Any] = {}
    facts["hostname"] = os.uname().nodename
    facts["kernel"] = os.uname().release
    facts["uptime_sec"] = int(float(_read("/proc/uptime").split()[0]) if _read("/proc/uptime") else 0)
    facts["loadavg"] = _read("/proc/loadavg")
    mem = _read("/proc/meminfo")
    if mem:
        for line in mem.splitlines():
            if line.startswith(("MemTotal", "MemAvailable", "SwapTotal")):
                k, v = line.split(":", 1)
                facts[k] = v.strip()
    facts["disk"] = _disk_usage()
    return facts


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def _disk_usage() -> list[dict]:
    out = []
    rc, so, _ = config.run(["df", "-hP"], timeout=15)
    for line in so.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6:
            try:
                used_pct = int(parts[4].rstrip("%"))
            except ValueError:
                used_pct = -1
            out.append({"fs": parts[0], "size": parts[1], "used": parts[2],
                        "avail": parts[3], "use%": used_pct, "mount": parts[5]})
    return out


def gpu_facts() -> dict:
    g = gpu_state()
    return g.to_dict()


def failing_units(scope: str = "--user") -> list[dict]:
    out = []
    rc, so, _ = config.run(["systemctl", scope, "list-units", "--state=failed",
                            "--no-pager", "--no-legend"], timeout=15)
    for line in so.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4:
            out.append({"unit": parts[0], "load": parts[1], "active": parts[2],
                        "sub": parts[3], "desc": parts[4] if len(parts) > 4 else ""})
    return out


def coredump_facts(since: str = "-2h") -> list[dict]:
    """从 journald 里捞崩溃记录。systemd-coredump 未安装时也能靠字段命中。"""
    out = []
    rc, so, _ = config.run(["journalctl", "--since", since, "-o", "json",
                            "--no-pager"], timeout=30)
    for line in so.splitlines():
        if "CODE_DUMPED" not in line and "COREDUMP" not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        out.append({
            "unit": obj.get("_SYSTEMD_UNIT"),
            "comm": obj.get("_COMM"),
            "exe": obj.get("_EXE"),
            "signal": obj.get("CODE_DUMPED"),
            "exit_status": obj.get("COREDUMP_EXIT_STATUS"),
            "message": (obj.get("MESSAGE") or "")[:500],
            "time": obj.get("__REALTIME_TIMESTAMP"),
        })
    return out[-20:]


def recent_errors(since: str = "-1h", limit: int = 40) -> list[dict]:
    out = []
    rc, so, _ = config.run(["journalctl", "--since", since, "-p", "err",
                            "-o", "json", "--no-pager"], timeout=30)
    for line in so.splitlines()[-limit:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        out.append({
            "unit": obj.get("_SYSTEMD_UNIT"),
            "identifier": obj.get("SYSLOG_IDENTIFIER"),
            "priority": obj.get("PRIORITY"),
            "message": (obj.get("MESSAGE") or "")[:400],
        })
    return out


def kernel_facts(since: str = "-1h") -> list[str]:
    rc, so, _ = config.run(["journalctl", "-k", "--since", since, "-p", "err",
                            "--no-pager"], timeout=20)
    return [l for l in so.splitlines() if l.strip()][-30:]


def build_context(kind: str = "generic", since: str = "-1h",
                  hint: dict | None = None) -> dict:
    """构建一次诊断的完整事实快照。"""
    ctx: dict[str, Any] = {
        "kind": kind,
        "ts": time.time(),
        "since": since,
        "system": system_facts(),
        "gpu": gpu_facts(),
        "failing_units": failing_units(),
        "coredumps": coredump_facts(since),
        "errors": recent_errors(since),
        "kernel": kernel_facts(since),
    }
    try:
        ctx["resident_models"] = Broker().residency()
    except Exception:
        ctx["resident_models"] = []
    if hint:
        ctx["hint"] = hint
    return ctx


def render_context(ctx: dict) -> str:
    """把事实快照渲染成给 LLM 看的紧凑文本。"""
    out = []
    out.append(f"诊断类型: {ctx.get('kind')}   时间窗: {ctx.get('since')}")
    s = ctx.get("system", {})
    out.append(f"主机 {s.get('hostname')} 内核 {s.get('kernel')} "
               f"负载 {s.get('loadavg')} 内存 {s.get('MemAvailable','?')}")
    g = ctx.get("gpu", {})
    out.append(f"GPU {g.get('name')} 显存 {g.get('used_mb')}/{g.get('total_mb')}MB "
               f"利用率 {g.get('utilization')}% 温度 {g.get('temp_c')}C")
    if ctx.get("failing_units"):
        out.append("失败单元: " + ", ".join(u["unit"] for u in ctx["failing_units"]))
    if ctx.get("coredumps"):
        out.append(f"崩溃记录 {len(ctx['coredumps'])} 条:")
        for c in ctx["coredumps"][-5:]:
            out.append(f"  - {c.get('unit') or c.get('comm')} signal={c.get('signal')} "
                       f"exit={c.get('exit_status')} :: {(c.get('message') or '')[:200]}")
    if ctx.get("errors"):
        out.append(f"错误日志 {len(ctx['errors'])} 条（最近 5 条）:")
        for e in ctx["errors"][-5:]:
            out.append(f"  - [{e.get('identifier')}] {(e.get('message') or '')[:200]}")
    if ctx.get("kernel"):
        out.append("内核错误:")
        for k in ctx["kernel"][-3:]:
            out.append(f"  - {k[:200]}")
    if ctx.get("resident_models"):
        out.append("驻留模型: " + ", ".join(f"{r['name']}({r['size_mb']}MB)"
                                            for r in ctx["resident_models"]))
    return "\n".join(out)
