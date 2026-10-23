"""nexus —— 对话式控制面。

设计对齐 Omarchy 的 `omarchy` 单命令范式：所有系统能力收敛到一个入口，
并且 `nexus "<自然语言>"` 能直接驱动系统改变状态（不是只给建议）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time

from . import audit, config, doctor, events, layering, sandbox, update, version as version_mod
from .agent import build_system_prompt, get_adapter, run_agent
from .broker import Broker, gpu_state
from .gateway import Gateway
from .mcp import MCPClient, MCPServer, Policy, ToolRegistry
from .registry import Registry, current_registry

COMMANDS = ("status", "doctor", "models", "gpu", "events", "agent", "mcp",
            "logs", "reset", "install", "explain", "gateway", "audit",
            "update", "channel", "version")


# ---------------------------------------------------------------- 展示helper


def _table(rows: list[dict], cols: list[tuple[str, str]], empty: str = "(空)") -> str:
    if not rows:
        return empty
    widths = {}
    for key, header in cols:
        widths[key] = max(len(header), *(len(str(r.get(key, ""))) for r in rows))
    out = ["  ".join(h.ljust(widths[k]) for k, h in cols)]
    out.append("  ".join("-" * widths[k] for k, _ in cols))
    for r in rows:
        out.append("  ".join(str(r.get(k, "")).ljust(widths[k]) for k, _ in cols))
    return "\n".join(out)


def _yes(b) -> str:
    return "是" if b else "否"


# ---------------------------------------------------------------- 子命令


def cmd_status(args) -> int:
    reg = current_registry()
    gw = Gateway(reg)
    br = Broker(reg)
    layer = layering.get_backend().status()
    gpu = gpu_state()

    print("╭─ NexusOS 状态 " + "─" * 46)
    print(f"│ 版本        : 0.2.0   运行时长: {doctor.system_facts().get('uptime_sec', 0)}s")
    print(f"│ 分层后端    : {layer.get('backend')}   基底 {layer.get('base')}")
    print(f"│ 数据目录    : {config.data_home()}")
    print(f"│ 配置目录    : {config.config_home()}")
    print(f"│ 配置来源    : " + ", ".join(
        f"{s['scope']}{'(有)' if s['exists'] else '(无)'}:{s['path']}"
        for s in config.config_sources("models.toml")))
    print(f"│ GPU         : {gpu.name}  {gpu.used_mb}/{gpu.total_mb}MB 已用  "
          f"空闲 {gpu.free_mb}MB  util {gpu.utilization}%")
    print(f"│ 显存预算    : {max(0, gpu.total_mb - reg.reserve_mb)}MB "
          f"(预留 {reg.reserve_mb}MB)")
    resident = br.residency()
    print(f"│ 驻留模型    : " + (", ".join(f"{r['name']}({r['size_mb']}MB)" for r in resident)
                                if resident else "无"))
    print("│ 后端健康    :")
    for h in [br.backend_health(b) for b in reg.backends]:
        print(f"│   - {h['backend']:<8} {_yes(h.get('ok'))}  {h.get('url', '')}"
              f"{'' if h.get('ok') else '  ← ' + str(h.get('reason'))[:60]}")
    print("│ MCP 工具    : " + ", ".join(
        sorted({t["name"].split(".")[0] for t in ToolRegistry(Policy()).list_tools()})))
    units = []
    rc, out, _ = config.run(["systemctl", "--user", "list-units", "nexus-*",
                              "--all", "--no-pager", "--no-legend"], timeout=10)
    for line in out.splitlines():
        p = line.split(None, 3)
        if len(p) >= 3:
            units.append({"unit": p[0], "active": p[2]})
    print("│ Nexus 单元  : " + (", ".join(f"{u['unit']}={u['active']}" for u in units)
                                or "未安装"))
    print("╰" + "─" * 62)
    return 0


def cmd_gpu(args) -> int:
    br = Broker()
    snap = br.snapshot()
    g = snap["gpu"]
    print(f"GPU: {g.get('name')}")
    print(f"  显存: {g.get('used_mb')}/{g.get('total_mb')} MB  空闲 {g.get('free_mb')} MB"
          f"  利用率 {g.get('utilization')}%  {g.get('temp_c')}C  {g.get('power_w')}W")
    print(f"  预算: {snap['budget_mb']} MB (预留 {snap['reserve_mb']} MB)")
    print(f"  驻留: {snap['resident'] or '无'}")
    print()
    print(_table(snap["models"],
                 [("name", "模型"), ("vram_mb", "显存MB"), ("params", "参数"),
                  ("quant", "量化"), ("fit", "适配"), ("fit_reason", "判定依据")]))
    return 0


def cmd_models(args) -> int:
    reg = current_registry()
    br = Broker(reg)
    if args.action == "list":
        g = gpu_state()
        rows = []
        for m in reg.models:
            v = br.fit(m, g)
            rows.append({"name": m.name, "backend": m.backend, "vram_mb": m.vram_mb,
                         "params": m.params, "quant": m.quant, "fit": v["fit"],
                         "reason": v["reason"], "tags": ",".join(m.tags)})
        print(_table(rows, [("name", "模型"), ("backend", "后端"), ("vram_mb", "显存MB"),
                            ("params", "参数"), ("quant", "量化"), ("fit", "适配"),
                            ("tags", "标签")]))
        print("\n适配档位: gpu=全部驻留显存 / partial=部分卸载到CPU / cpu=只能CPU推理")
        return 0
    if args.action == "route":
        gw = Gateway(reg)
        print(json.dumps(gw.resolve_route(args.name or ""), ensure_ascii=False, indent=2))
        return 0
    if args.action == "discover":
        from .registry import discover_from_backend
        found = []
        for b in reg.backends:
            found += discover_from_backend(b, reg.backend_url(b))
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0
    if args.action == "load":
        res = br.load(args.name, force=args.force)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if not res.get("ok"):
            for tip in res.get("advice", []):
                print(f"  → {tip}")
            return 1
        return 0
    if args.action == "unload":
        print(json.dumps(br.unload(args.name), ensure_ascii=False, indent=2))
        return 0
    if args.action == "stage":
        res = br.stage(args.name)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    print("未知子动作")
    return 2


def cmd_doctor(args) -> int:
    ctx = doctor.build_context(kind="manual", since=args.since)
    print(doctor.render_context(ctx))
    print()
    if args.json:
        print(json.dumps(ctx, ensure_ascii=False, indent=2, default=str))
    if args.agent:
        result = run_agent(f"请诊断这台机器当前状态，给出最需要关注的三点。\n\n"
                           + doctor.render_context(ctx),
                           adapter=args.adapter, sandbox_tier="standard")
        print("\n=== Agent 诊断 ===")
        print(result.output or result.error)
    return 0


def cmd_events(args) -> int:
    if args.action == "list":
        rs = events.RuleSet.load()
        rows = [{"name": r.name, "description": r.description, "action": r.action,
                 "adapter": r.adapter, "match": r.match_regex or ("字段:" + ",".join(r.match_fields)),
                 "priority": r.priority} for r in rs.rules]
        print(_table(rows, [("name", "规则"), ("match", "匹配"), ("action", "动作"),
                            ("adapter", "适配器"), ("priority", "优先级")]))
        print()
        print("最近命中:")
        for h in events.events_global_hits()[-10:]:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(h['ts']))} "
                  f"{h['rule']}: {str(h.get('message', ''))[:100]}")
        print()
        print("诊断归档:")
        for i in events.list_incidents():
            print(f"  {i.get('id')}  rule={i.get('rule')}  ok={i.get('ok')}  {i.get('md')}")
        return 0
    if args.action == "test":
        rs = events.RuleSet.load()
        rule = next((r for r in rs.rules if r.name == args.rule), None)
        if rule is None:
            print(f"没有规则 {args.rule}")
            return 2
        entry = events.synthesize(args.rule,
                                  MESSAGE=args.message or
                                  f"nexus selftest synthetic event for rule {args.rule}")
        print(f"→ 合成事件命中规则 {rule.name}，开始构建事实快照并派发 agent…")
        res = events.EventRouter(rs).diagnose(rule, entry, adapter=args.adapter)
        print(json.dumps({k: v for k, v in res.items() if k != "output"},
                         ensure_ascii=False, indent=2))
        print("\n=== Agent 诊断 ===")
        print(res.get("output") or res.get("error"))
        return 0
    if args.action == "follow":
        print("跟随 journald，命中规则即自动诊断（Ctrl-C 退出）…")
        events.EventRouter().follow(since=args.since)
        return 0
    print("未知子动作")
    return 2


def cmd_agent(args) -> int:
    adp = get_adapter(args.adapter)
    print(f"适配器 {args.adapter} 可用: {_yes(adp.available())}")
    if args.explain:
        print(json.dumps(sandbox.describe(args.tier), ensure_ascii=False, indent=2))
        return 0
    if not args.task:
        print("需要任务描述")
        return 2
    task = " ".join(args.task) if isinstance(args.task, list) else str(args.task)
    res = run_agent(task, adapter=args.adapter, sandbox_tier=args.tier,
                    model=args.model, max_iterations=args.max_iterations)
    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(res.output or res.error)
        if res.tool_calls:
            print(f"\n工具调用 {len(res.tool_calls)} 次: " +
                  ", ".join(c["tool"] for c in res.tool_calls))
    return 0 if res.ok else 1


def cmd_mcp(args) -> int:
    client = MCPClient(ToolRegistry(Policy()))
    if args.action == "list":
        for t in client.registry.list_tools():
            print(f"{t['name']:<22} {t['description']}")
        print(f"\n共 {len(client.registry.list_tools())} 个工具")
        return 0
    if args.action == "test":
        print("=== initialize ===")
        srv = MCPServer()
        resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05"}})
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        print("=== tools/list (前 3 个) ===")
        resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        print(json.dumps(tools[:3], ensure_ascii=False, indent=2))
        print(f"... 共 {len(tools)} 个")
        print("=== tools/call gpu.snapshot ===")
        resp = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "gpu.snapshot", "arguments": {}}})
        content = resp["result"]["content"][0]["text"]
        print(content[:600])
        print("\n=== 策略边界测试（写 /etc 应被拒绝） ===")
        resp = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                           "params": {"name": "fs.write",
                                      "arguments": {"path": "/etc/nexus-test",
                                                    "content": "x"}}})
        print(resp["result"]["content"][0]["text"][:300])
        return 0
    print("未知子动作")
    return 2


def cmd_logs(args) -> int:
    if args.which == "audit":
        for e in audit.read_recent(args.n):
            print(json.dumps(e, ensure_ascii=False))
        return 0
    if args.which == "events":
        p = config.log_dir() / "events" / f"{time.strftime('%Y-%m-%d')}.jsonl"
        if not p.exists():
            print("(无事件日志)")
            return 0
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-args.n:]:
            print(line)
        return 0
    rc, out, _ = config.run(["journalctl", "--user", "-u", "nexus-event-router",
                              "-n", str(args.n), "--no-pager"], timeout=15)
    print(out)
    return 0


def cmd_audit(args) -> int:
    counts = audit.count_by_kind()
    print(_table([{"kind": k, "count": v} for k, v in sorted(counts.items())],
                 [("kind", "事件类型"), ("count", "次数")]))
    print(f"\n审计文件: {audit.audit_path()}")
    return 0


def cmd_reset(args) -> int:
    layer = layering.get_backend()
    st = layer.reset(dry_run=args.dry_run)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    if not args.dry_run:
        print("已回到底层基底状态。")
    return 0


def cmd_install(args) -> int:
    print("bootstrap 请在 shell 中执行: nexus-bootstrap")
    print("或: python -m nexus.install")
    return 0


def cmd_explain(args) -> int:
    print(json.dumps(sandbox.describe(args.tier), ensure_ascii=False, indent=2))
    print()
    print("=== Agent system prompt（实际注入内容）===")
    print(build_system_prompt())
    return 0


def cmd_version(args) -> int:
    info = version_mod.installed()
    src = version_mod.source_version()
    print(f"已安装版本 : {info.version}   通道: {info.channel}")
    print(f"安装时间   : " + (time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(info.installed_at))
                             if info.installed_at else "未知"))
    if info.commit:
        print(f"commit     : {info.commit}")
    if src and src != info.version:
        print(f"源码树版本 : {src}")
    rep = update.pending_report()
    if rep["migrations_pending"]:
        print(f"待执行迁移 : {len(rep['migrations_pending'])} 个 → "
              + ", ".join(rep["migrations_pending"]))
    else:
        print("待执行迁移 : 无")
    return 0


def cmd_channel(args) -> int:
    from . import version as v
    if args.action == "current":
        print(v.installed().channel)
        return 0
    if args.action == "list":
        print(_table([{"ch": c, "src": v.CHANNEL_SOURCES[c]} for c in v.CHANNELS],
                     [("ch", "通道"), ("src", "产物来源")]))
        print("\nstable = 官方发布版（推荐）")
        print("rc     = 大版本前的候选验证版")
        print("edge   = 最新开发构建（可能不稳定）")
        print("dev    = 直接使用本地源码树（需要你自己有 checkout）")
        return 0
    if args.action == "set":
        try:
            info = v.set_channel(args.name)
        except ValueError as exc:
            print(str(exc))
            return 2
        audit.record("channel.set", channel=args.name)
        print(f"通道已切换到 {info.channel}")
        print("下次 `nexus update` 将从该通道获取产物。")
        return 0
    print("未知子动作")
    return 2


def cmd_update(args) -> int:
    if args.migrate_only:
        rep = update.pending_report()
        print(f"待执行迁移 {len(rep['migrations_pending'])} 个")
        res = update.run_migrations(dry_run=args.dry_run)
        for r in res["ran"]:
            print(f"  {r['migration']}: {r['status']}")
        return 0
    print(f"→ 更新通道: {args.channel or version_mod.installed().channel}")
    res = update.perform_update(channel=args.channel, dry_run=args.dry_run,
                                 skip_fetch=args.skip_fetch)
    for st in res.get("steps", []):
        mark = "✓" if st.get("ok") else "✗"
        print(f"  {mark} {st['step']}: " +
              " ".join(f"{k}={v}" for k, v in st.items() if k not in ("step", "ok")))
    if not res.get("ok"):
        print(f"\n更新未完成: {res.get('error')}")
        if res.get("hint"):
            print(f"提示: {res['hint']}")
        if res.get("snapshot"):
            print(f"回滚点: {res['snapshot']}")
        return 1
    print(f"\n更新完成。回滚点: {res.get('snapshot')}")
    return 0


def cmd_gateway(args) -> int:
    from .gateway import serve
    print(f"网关启动于 http://{args.host}:{args.port}  (OpenAI 兼容)")
    serve(args.host, args.port)
    return 0


def cmd_dialogue(args) -> int:
    """自然语言入口：nexus "<任意指令>"。"""
    task = " ".join(args.task)
    print(f"→ 交给 agent（适配器 {args.adapter}，沙箱 {args.tier}）…\n")
    res = run_agent(task, adapter=args.adapter, sandbox_tier=args.tier,
                    model=args.model, max_iterations=args.max_iterations)
    print(res.output or res.error)
    if res.tool_calls:
        print(f"\n── 工具调用 ({len(res.tool_calls)}) ──")
        for c in res.tool_calls:
            status = "ok" if c.get("ok") else f"失败: {c.get('error')}"
            print(f"  {c['tool']}({json.dumps(c.get('args', {}), ensure_ascii=False)}) → {status}")
    return 0 if res.ok else 1


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nexus",
                                description="NexusOS —— AI 原生系统控制面")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("status", help="系统总览")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("doctor", help="主动诊断")
    s.add_argument("--since", default="-1h")
    s.add_argument("--json", action="store_true")
    s.add_argument("--agent", action="store_true", help="同时让 agent 分析")
    s.add_argument("--adapter", default="local")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("gpu", help="显存状态与模型适配判定")
    s.set_defaults(func=cmd_gpu)

    s = sub.add_parser("models", help="模型注册表与显存仲裁")
    s.add_argument("action", choices=["list", "load", "unload", "route", "discover", "stage"])
    s.add_argument("name", nargs="?", default="")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("events", help="事件规则与诊断归档")
    s.add_argument("action", choices=["list", "test", "follow"])
    s.add_argument("rule", nargs="?", default="")
    s.add_argument("--message", default="")
    s.add_argument("--adapter", default="local")
    s.add_argument("--since", default="-1h")
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("agent", help="直接调用 agent")
    s.add_argument("task", nargs="*")
    s.add_argument("--adapter", default="local", choices=["local", "cli", "mock"])
    s.add_argument("--tier", default="strict", choices=list(sandbox.TIERS))
    s.add_argument("--model", default=None)
    s.add_argument("--max-iterations", type=int, default=6)
    s.add_argument("--json", action="store_true")
    s.add_argument("--explain", action="store_true")
    s.set_defaults(func=cmd_agent)

    s = sub.add_parser("mcp", help="MCP 工具自检")
    s.add_argument("action", choices=["list", "test"])
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("logs", help="查看日志")
    s.add_argument("which", nargs="?", default="audit", choices=["audit", "events", "router"])
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("audit", help="审计统计")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("reset", help="回到底层基底")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("install", help="安装引导")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("explain", help="展示信任边界与 system prompt")
    s.add_argument("--tier", default="strict", choices=list(sandbox.TIERS))
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser("gateway", help="前台启动 AI 网关")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(func=cmd_gateway)

    s = sub.add_parser("version", help="版本与迁移状态")
    s.set_defaults(func=cmd_version)

    s = sub.add_parser("channel", help="发布通道管理")
    s.add_argument("action", choices=["current", "list", "set"])
    s.add_argument("name", nargs="?", default="")
    s.set_defaults(func=cmd_channel)

    s = sub.add_parser("update", help="更新 NexusOS（唯一受支持的入口）")
    s.add_argument("--channel", default=None, choices=list(version_mod.CHANNELS))
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--skip-fetch", action="store_true", help="只跑迁移，不拉产物")
    s.add_argument("--migrate-only", action="store_true")
    s.set_defaults(func=cmd_update)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `nexus agent run "<任务>"` 与 `nexus agent "<任务>"` 等价
    if len(argv) >= 2 and argv[0] == "agent" and argv[1] == "run":
        argv = [argv[0]] + argv[2:]
    # 自然语言入口：首个 token 不是已知命令 → 当作对话任务
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        p = argparse.ArgumentParser(prog="nexus")
        p.add_argument("task", nargs="+")
        p.add_argument("--adapter", default="local")
        p.add_argument("--tier", default="strict", choices=list(sandbox.TIERS))
        p.add_argument("--model", default=None)
        p.add_argument("--max-iterations", type=int, default=6)
        args = p.parse_args(argv)
        return cmd_dialogue(args)

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    config.ensure_dirs()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
