"""事件总线 —— 系统事件反向驱动 agent。

这是「AI 原生」真正的落点，也是 Omarchy 唯一被公认做出 OS 深度的那一层：
    systemd-coredump → agent 自动分析崩溃
本模块把它一般化成可配置的规则引擎：

    journald  →  规则匹配  →  事实快照  →  agent  →  诊断归档  →  可选自动处置

关键设计：
  * 事件总线常驻，但模型不常驻 —— agent 按需拉起，闲置后由 broker 卸载（显存友好）。
  * 规则在 events.toml 里声明式定义，用户可自己加。
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Callable

from . import audit, config, doctor, permissions
from .agent import run_agent

DEFAULT_RULES: list[dict] = [
    {
        "name": "crash",
        "description": "进程崩溃自诊断（对应 Omarchy 的 coredump→agent）",
        "match_fields": ["CODE_DUMPED", "COREDUMP_EXIT_STATUS"],
        "match_regex": r"status=\d+/(0x[0-9a-fA-F]+|core dump)",
        "scope": "any",
        "action": "diagnose",
        "adapter": "local",
        "priority": 10,
    },
    {
        "name": "gpu_oom",
        "description": "GPU 显存耗尽",
        "match_regex": r"CUDA out of memory|out of memory.*GPU|failed to allocate.*MiB",
        "scope": "any",
        "action": "diagnose",
        "adapter": "local",
        "priority": 20,
    },
    {
        "name": "disk_full",
        "description": "磁盘写满",
        "match_regex": r"No space left on device|ENOSPC",
        "scope": "any",
        "action": "diagnose",
        "adapter": "local",
        "priority": 30,
    },
    {
        "name": "unit_failed",
        "description": "systemd 单元失败",
        "match_regex": r"Failed to start .*\.service|Main process exited, status=",
        "scope": "system",
        "action": "diagnose",
        "adapter": "local",
        "priority": 40,
    },
    {
        "name": "kernel_oops",
        "description": "内核级严重错误",
        "match_regex": r"Oops|BUG:|kernel panic",
        "scope": "kernel",
        "action": "diagnose",
        "adapter": "local",
        "priority": 50,
    },
]


_GLOBAL_HITS: list[dict] = []


def events_global_hits() -> list[dict]:
    """模块级命中记录（供 CLI 查询）。"""
    return list(_GLOBAL_HITS)


def record_hit(rule_name: str, message: str) -> None:
    _GLOBAL_HITS.append({"ts": time.time(), "rule": rule_name, "message": message[:200]})
    del _GLOBAL_HITS[:-200]


@dataclasses.dataclass
class Rule:
    name: str
    description: str = ""
    match_fields: list[str] = dataclasses.field(default_factory=list)
    match_regex: str = ""
    match_priority: int = 0
    scope: str = "any"          # any | kernel | system | user
    action: str = "diagnose"
    adapter: str = "local"
    priority: int = 100

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def scope_ok(self, entry: dict) -> bool:
        """作用域过滤 —— 避免在无关日志上误报。"""
        if self.scope == "any":
            return True
        is_kernel = (entry.get("_TRANSPORT") == "kernel"
                     or entry.get("SYSLOG_IDENTIFIER") == "kernel")
        is_user = bool(entry.get("_SYSTEMD_USER_UNIT") or entry.get("USER_UNIT"))
        if self.scope == "kernel":
            return is_kernel
        if self.scope == "user":
            return is_user
        if self.scope == "system":
            return not is_user
        return True


class RuleSet:
    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules = rules or []

    @classmethod
    def load(cls) -> "RuleSet":
        data = config.load_layered("events.toml", {})
        raw = data.get("rule") or DEFAULT_RULES
        return cls([Rule.from_dict(r) for r in raw])

    def match(self, entry: dict) -> Rule | None:
        """把一条 journald 事件匹配到规则。命中多条时取 priority 最高（数字小）的。"""
        message = str(entry.get("MESSAGE", ""))
        hits = []
        for rule in self.rules:
            if not rule.scope_ok(entry):
                continue
            if rule.match_fields:
                if any(f in entry for f in rule.match_fields):
                    hits.append(rule)
                    continue
            if rule.match_regex:
                try:
                    if re.search(rule.match_regex, message):
                        hits.append(rule)
                        continue
                except re.error:
                    pass
            if rule.match_priority:
                try:
                    if int(entry.get("PRIORITY", 99)) <= rule.match_priority:
                        hits.append(rule)
                        continue
                except (TypeError, ValueError):
                    pass
        if not hits:
            return None
        return sorted(hits, key=lambda r: r.priority)[0]


class EventRouter:
    """journald 订阅 + 规则匹配 + agent 派发。"""

    def __init__(self, ruleset: RuleSet | None = None,
                 on_event: Callable[[Rule, dict], None] | None = None,
                 debounce_sec: int = 300) -> None:
        self.ruleset = ruleset or RuleSet.load()
        self.on_event = on_event
        self.debounce_sec = debounce_sec
        self.hits: list[dict] = []
        self._stop = threading.Event()
        self._last_fired: dict[tuple, float] = {}

    # -- 去抖 ---------------------------------------------------------
    def suppressed(self, rule: Rule, entry: dict) -> bool:
        """同一条规则 + 同一单元在冷却期内不重复派发。

        没有去抖的话，一个反复重启的单元会把模型反复唤醒烧显存。
        """
        key = (rule.name, entry.get("_SYSTEMD_UNIT") or entry.get("_SYSTEMD_USER_UNIT") or "")
        now = time.time()
        last = self._last_fired.get(key, 0.0)
        if now - last < self.debounce_sec:
            audit.record("event.suppressed", rule=rule.name, key=key[1],
                         since=round(now - last))
            return True
        self._last_fired[key] = now
        return False

    # -- 匹配 ---------------------------------------------------------
    def handle_entry(self, entry: dict) -> dict | None:
        rule = self.ruleset.match(entry)
        if rule is None:
            return None
        hit = {"ts": time.time(), "rule": rule.name, "entry": {
            k: entry.get(k) for k in ("_SYSTEMD_UNIT", "SYSLOG_IDENTIFIER",
                                      "PRIORITY", "MESSAGE", "CODE_DUMPED",
                                      "COREDUMP_EXIT_STATUS")}}
        self.hits.append(hit)
        self.hits = self.hits[-200:]
        audit.record("event.match", rule=rule.name,
                     unit=entry.get("_SYSTEMD_UNIT"),
                     message=str(entry.get("MESSAGE", ""))[:200])
        if self.on_event:
            try:
                self.on_event(rule, entry)
            except Exception as exc:
                audit.record("event.handler_error", rule=rule.name, error=str(exc))
        return hit

    # -- 派发 ---------------------------------------------------------
    def diagnose(self, rule: Rule, entry: dict, *, adapter: str | None = None,
                 auto: bool = False) -> dict:
        """为一条命中规则构建事实快照并交给 agent。"""
        ctx = doctor.build_context(kind=rule.name, hint={
            "rule": rule.name,
            "unit": entry.get("_SYSTEMD_UNIT"),
            "message": str(entry.get("MESSAGE", ""))[:1000],
            "signal": entry.get("CODE_DUMPED"),
        })
        rendered = doctor.render_context(ctx)
        task = (f"系统触发了『{rule.name}』事件（{rule.description}）。\n"
                f"请基于下面的系统事实诊断根因并给出可执行的修复步骤。\n\n"
                f"{rendered}")
        adapter = adapter or rule.adapter
        result = run_agent(task, adapter=adapter,
                           sandbox_tier=permissions.resolve_tier())

        incident_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{rule.name}"
        d = config.incidents_dir() / time.strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{incident_id}.json").write_text(
            json.dumps({"id": incident_id, "rule": rule.name, "context": ctx,
                        "result": result.to_dict()}, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n")
        (d / f"{incident_id}.md").write_text(
            f"# 事件诊断 {incident_id}\n\n## 事实快照\n\n```\n{rendered}\n```\n\n"
            f"## Agent 结论\n\n{result.output or result.error}\n",
            encoding="utf-8", newline="\n")
        audit.record("incident.diagnosed", rule=rule.name, incident=incident_id,
                     ok=result.ok, tools=[c["tool"] for c in result.tool_calls])
        return {"incident_id": incident_id, "rule": rule.name,
                "ok": result.ok, "output": result.output, "error": result.error,
                "path": str(d / f"{incident_id}.md")}

    # -- 订阅 ---------------------------------------------------------
    def follow(self, since: str = "-1h") -> None:
        """跟随 journald，把命中事件交给诊断链路。"""
        proc = subprocess.Popen(
            ["journalctl", "--since", since, "-f", "-o", "json", "--no-pager"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            bufsize=1)
        audit.record("event.follow.start", pid=proc.pid, since=since)
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if self._stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rule = self.ruleset.match(entry)
                if rule is None:
                    continue
                if self.suppressed(rule, entry):
                    continue
                self.hits.append({"ts": time.time(), "rule": rule.name,
                                  "message": str(entry.get("MESSAGE", ""))[:200]})
                self.hits = self.hits[-200:]
                audit.record("event.match", rule=rule.name,
                             unit=entry.get("_SYSTEMD_UNIT"))
                try:
                    self.diagnose(rule, entry)
                except Exception as exc:
                    audit.record("event.diagnose_error", rule=rule.name, error=str(exc))
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            audit.record("event.follow.stop", pid=proc.pid)

    def stop(self) -> None:
        self._stop.set()


def synthesize(rule_name: str, **overrides) -> dict:
    """构造一条模拟的 journald 事件（用于自测与演示）。"""
    base = {
        "_SYSTEMD_UNIT": "nexus-selftest.service",
        "SYSLOG_IDENTIFIER": "nexus-selftest",
        "PRIORITY": "3",
        "MESSAGE": "nexus selftest synthetic event",
    }
    base.update(overrides)
    return base


def list_incidents(limit: int = 20) -> list[dict]:
    d = config.incidents_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.rglob("*.json"), reverse=True)[:limit]:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": obj.get("id"), "rule": obj.get("rule"),
                        "path": str(p), "md": str(p.with_suffix(".md")),
                        "ok": obj.get("result", {}).get("ok")})
        except Exception:
            out.append({"path": str(p)})
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="NexusOS event router")
    ap.add_argument("--since", default="-1h")
    ap.add_argument("--test", default="", help="只测一条规则，不订阅")
    a = ap.parse_args()
    router = EventRouter()
    if a.test:
        rule = next((r for r in router.ruleset.rules if r.name == a.test), None)
        if rule is None:
            print(f"没有规则 {a.test}")
            raise SystemExit(2)
        print(json.dumps(router.diagnose(rule, synthesize(a.test)),
                         ensure_ascii=False, indent=2))
    else:
        router.follow(since=a.since)
