"""新手引导 —— 新用户第一次进来时走一遍核心能力。

设计原则：**每一步都真的执行一次命令并展示真实输出**，而不是打印一段说明书。
引导本身也是对系统的自检 —— 跑挂了用户立刻就知道，而不是等到用的时候才炸。

进度记在 $NEXUS_DATA_HOME/state/tutorial.json：
  · 第一次进来（`nexus` 无参数）→ 完整引导
  · 之后再进来 → 简短回顾 + 关键命令
  · `nexus tutorial` → 随时重跑完整引导
  · `nexus tutorial --reset` → 清进度，下次进来重新走完整流程
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

from . import config, permissions

STATE_NAME = "tutorial.json"


# ---------------------------------------------------------------- 步骤定义


def _steps() -> list[dict]:
    return [
        {
            "key": "status",
            "title": "系统总览",
            "why": "先看全局：版本、GPU 占用、显存预算、驻留模型、后台服务是否都活着。",
            "cmd": ["status"],
            "expect": "一张表，最后一行是 4 个 nexus 单元 —— 它们全是 active 才说明系统层在跑。",
        },
        {
            "key": "gpu",
            "title": "真实显存",
            "why": "这是台 GPU 工作站，模型能不能跑完全取决于显存怎么分。",
            "cmd": ["gpu"],
            "expect": "显存占用/总量/空闲 + 一张模型表，每行标注 gpu/partial/cpu 三档适配。",
        },
        {
            "key": "models",
            "title": "模型注册表",
            "why": "注册表是唯一真相源：既知道现在挂着谁，也知道共享仓库里有哪些可选。",
            "cmd": ["models", "list"],
            "expect": "每个模型带显存占用、参数量、量化与适配档位。",
        },
        {
            "key": "dialogue",
            "title": "对话式控制面",
            "why": "NexusOS 和「装了 Ollama 的 Ubuntu」的区别就在这：自然语言直接驱动系统改变状态。",
            "cmd": ["agent", "run", "--adapter", "mock", "检查一下系统状态"],
            "expect": "（这里用 mock 适配器保证秒级）真实调用会拉起本地模型，约 25 秒。",
        },
        {
            "key": "events",
            "title": "事件自动诊断",
            "why": "系统出故障时，事件总线会自己抓现场、派 agent 诊断、归档成事故报告。",
            "cmd": ["events", "list"],
            "expect": "5 条内置规则：crash / gpu_oom / disk_full / unit_failed / kernel_oops。",
        },
        {
            "key": "permissions",
            "title": "你的权限档位",
            "why": "agent 能碰哪些目录、能执行什么命令，由这个档位决定。随时可调。",
            "cmd": ["permissions"],
            "expect": "当前档位 + 可写根 + systemd 沙箱属性。改档位：nexus permissions set open",
        },
    ]


# ---------------------------------------------------------------- 状态


def _state_path() -> pathlib.Path:
    return config.state_dir() / STATE_NAME


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def reset() -> None:
    p = _state_path()
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------- 执行


def _run_cli(args: list[str], timeout: int = 120) -> tuple[int, str]:
    """跑一次真实的 nexus CLI，展示用户会看到的输出。"""
    env = dict(os.environ)
    lib = str(pathlib.Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = lib + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        p = subprocess.run([sys.executable, "-m", "nexus.cli"] + args,
                           capture_output=True, text=True, timeout=timeout,
                           env=env, cwd=str(pathlib.Path.home()))
        return p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        return 124, "(超时)"
    except Exception as exc:
        return 1, f"(执行失败: {exc})"


def _truncate(text: str, lines: int = 14) -> str:
    ls = [l for l in text.splitlines() if l.strip()]
    if len(ls) <= lines:
        return "\n".join(ls)
    return "\n".join(ls[:lines] + [f"  …（还有 {len(ls) - lines} 行）"])


# ---------------------------------------------------------------- 主流程


def run(reset: bool = False, quiet: bool = False) -> int:
    if reset:
        reset_state()
    state = _load_state()
    first_run = not state.get("done_at")

    if not quiet:
        _banner(first_run)

    if not first_run:
        # 已经走过一遍：只给回顾，不再把 6 步重跑一遍（每次 30 秒，没人愿意反复等）
        _recap()
        _save_state({"done_at": state.get("done_at", time.time()),
                     "runs": int(state.get("runs", 0)) + 1,
                     "steps": state.get("steps", [])})
        return 0

    steps = _steps()
    for i, st in enumerate(steps, 1):
        if not quiet:
            print(f"\n\033[1m[{i}/{len(steps)}] {st['title']}\033[0m")
            print(f"  为什么看这个: {st['why']}")
            print(f"  执行: nexus {' '.join(st['cmd'])}")
        rc, out = _run_cli(st["cmd"])
        if out.strip():
            print(_indent(_truncate(out)))
        if not quiet:
            print(f"  \033[2m→ {st['expect']}\033[0m")
        if rc not in (0, 1):
            print(f"  \033[33m→ 这条命令返回 {rc}，可能有异常\033[0m")

    _save_state({"done_at": time.time(), "runs": int(state.get("runs", 0)) + 1,
                 "steps": [s["key"] for s in steps]})

    if not quiet:
        _closing(first_run)
    return 0


def _recap() -> None:
    """已走过引导的用户：给一张速查表，不重复执行。"""
    tier = permissions.resolve_tier()
    print("\n常用命令")
    print("-" * 62)
    for cmd, desc in [
        ("nexus status", "系统总览（版本/GPU/服务）"),
        ("nexus gpu", "显存占用与模型适配判定"),
        ("nexus models list", "模型注册表"),
        ('nexus "把没用的模型卸载掉"', "对话式驱动系统改变状态"),
        ("nexus events test crash", "看事件总线自动诊断"),
        ("nexus permissions", "当前权限档位"),
        ("nexus permissions set open", "放开权限（谨慎）"),
        ("nexus tutorial", "重新走一遍完整引导"),
    ]:
        print(f"  {cmd:<38} {desc}")
    print(f"\n当前权限档位: {tier}  ({permissions.TIER_LABEL[tier]})")
    print("详细手册: docs/04-PLAYBOOK.md")


def reset_state() -> None:
    reset()


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + l for l in text.splitlines())


def _banner(first_run: bool) -> None:
    print("╭" + "─" * 62)
    if first_run:
        print("│  欢迎使用 NexusOS —— AI 原生的 Linux 系统层")
        print("│")
        print("│  下面 6 步会真的执行命令并展示真实输出，大约 30 秒。")
        print("│  任何时候想重来：nexus tutorial")
    else:
        print("│  NexusOS 快速回顾")
        print("│")
        print("│  下面是核心命令与真实输出。想看完整引导：nexus tutorial")
    print("╰" + "─" * 62)


def _closing(first_run: bool) -> None:
    tier = permissions.resolve_tier()
    print("\n" + "╭" + "─" * 62)
    print("│  引导完成")
    print("│")
    print(f"│  当前权限档位: {tier}  ({permissions.TIER_LABEL[tier]})")
    print("│")
    print("│  接下来可以试试：")
    print("│    nexus \"把显卡上没用的模型卸载掉\"")
    print("│    nexus \"找出占用磁盘最多的目录\"")
    print("│    nexus events test crash        # 看事件总线自动诊断")
    print("│    nexus permissions set open      # 放开权限（谨慎）")
    print("│")
    print("│  完整手册: docs/04-PLAYBOOK.md")
    print("╰" + "─" * 62)
