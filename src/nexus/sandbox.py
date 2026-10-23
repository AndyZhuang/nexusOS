"""沙箱 —— 用 systemd 原生指令划定 agent 的能力边界。

为什么不用 bubblewrap：本机无外网装不了包，而 systemd 自带的沙箱指令零依赖、
与本 OS 的哲学一致。关键点：ReadWritePaths= 会覆盖 ProtectHome=read-only 的只读效果，
因此可以做到"家目录只读、仅 nexus 数据区可写"。

对应 docs/00-DESIGN-REVIEW.md H4（v0.1 的"软约束不阻断"等于 Omarchy 4.0 的漏洞形态）。

档位定义统一放在 permissions.py —— 沙箱层与 MCP 策略层由同一个 tier 驱动，
避免出现"沙箱说一套、策略做一套"（v0.2 的真实缺陷）。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
from typing import Sequence

from . import config, permissions

# 档位定义在 permissions 模块（唯一真相源），这里保留别名以兼容旧调用点
TIERS = permissions.TIERS


def _data_paths() -> list[str]:
    return [str(config.data_home()), str(config.config_home())]


def build_properties(tier: str = permissions.DEFAULT_TIER,
                     writable: Sequence[str] | None = None,
                     memory_max: str | None = None,
                     network: bool = True) -> list[str]:
    """构造 systemd-run 的 -p 属性列表。

    tier 决定基础属性；writable 是额外的可写路径（在档位允许的前提下叠加）。
    """
    if tier not in TIERS:
        tier = permissions.DEFAULT_TIER
    props: list[str] = permissions.sandbox_properties(tier)
    if tier == "off":
        return props

    if writable:
        # 档位默认之外再放行这些路径（显式叠加，不覆盖档位的只读区域）
        for p in writable:
            if p and os.path.exists(p) and f"ReadWritePaths={p}" not in props:
                props.append(f"ReadWritePaths={p}")

    if not network:
        props = [p for p in props if not p.startswith("RestrictAddressFamilies=")]
        props.append("RestrictAddressFamilies=AF_UNIX")
    if memory_max:
        props.append(f"MemoryMax={memory_max}")
    return props


def systemd_run_available() -> bool:
    return shutil.which("systemd-run") is not None


def run_sandboxed(cmd: Sequence[str], *, tier: str = "strict",
                  writable: Sequence[str] | None = None,
                  memory_max: str | None = None,
                  network: bool = True,
                  env: dict | None = None,
                  timeout: int = 900,
                  cwd: str | None = None,
                  stdin_data: str | None = None) -> tuple[int, str, str]:
    """在沙箱里执行命令，返回 (rc, stdout, stderr)。

    tier="off" 时直接执行（显式放开，需要调用方明确要求）。
    """
    e = dict(os.environ)
    if env:
        e.update(env)
    if tier == "off" or not systemd_run_available():
        try:
            p = subprocess.run(list(cmd), capture_output=True, text=True,
                               timeout=timeout, env=e, cwd=cwd, input=stdin_data)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout}s"
        except Exception as exc:
            return 1, "", str(exc)

    argv = ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect"]
    for prop in build_properties(tier, writable, memory_max, network):
        argv += ["-p", prop]
    if cwd:
        argv += ["--working-directory", cwd]
    argv += ["--"] + list(cmd)
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, env=e, input=stdin_data)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def describe(tier: str = permissions.DEFAULT_TIER) -> dict:
    """给 nexus status / agent --explain 展示当前边界。"""
    d = permissions.describe(tier)
    d["systemd_run"] = systemd_run_available()
    d["writable"] = _data_paths()
    d["note"] = ("档位由 nexus permissions 管理；家目录在 standard/open 档可写；"
                 "tier=off 表示显式放开边界（会被审计记录）")
    return d
