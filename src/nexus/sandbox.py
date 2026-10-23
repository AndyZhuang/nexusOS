"""沙箱 —— 用 systemd 原生指令划定 agent 的能力边界。

为什么不用 bubblewrap：本机无外网装不了包，而 systemd 自带的沙箱指令零依赖、
与本 OS 的哲学一致。关键点：ReadWritePaths= 会覆盖 ProtectHome=read-only 的只读效果，
因此可以做到"家目录只读、仅 nexus 数据区可写"。

对应 docs/00-DESIGN-REVIEW.md H4（v0.1 的"软约束不阻断"等于 Omarchy 4.0 的漏洞形态）。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
from typing import Sequence

from . import config

# 三档沙箱强度
TIERS = ("strict", "standard", "off")


def _data_paths() -> list[str]:
    return [str(config.data_home()), str(config.config_home())]


def build_properties(tier: str = "strict", writable: Sequence[str] | None = None,
                     memory_max: str | None = None,
                     network: bool = True) -> list[str]:
    """构造 systemd-run 的 -p 属性列表。"""
    if tier not in TIERS:
        tier = "standard"
    props: list[str] = []
    if tier == "off":
        return props

    props += [
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=no",
        "SystemCallArchitectures=native",
    ]
    if tier == "strict":
        props += [
            "ProtectSystem=strict",
            "ProtectHome=read-only",
        ]
        rw = list(writable or []) + _data_paths()
        # ReadWritePaths 覆盖 ProtectHome=read-only 的只读效果
        for p in rw:
            if p and os.path.exists(p):
                props.append(f"ReadWritePaths={p}")
        props.append("RestrictAddressFamilies=" +
                     ("AF_UNIX AF_INET AF_INET6" if network else "AF_UNIX"))
    else:
        props.append("ProtectSystem=full")
        props.append("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6")
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


def describe(tier: str = "strict") -> dict:
    """给 nexus status / agent --explain 展示当前边界。"""
    props = build_properties(tier)
    return {
        "tier": tier,
        "systemd_run": systemd_run_available(),
        "properties": props,
        "writable": _data_paths(),
        "note": ("tier=strict 时系统目录与家目录只读，仅 nexus 数据区可写；"
                 "tier=off 表示显式放开边界（会被审计记录）"),
    }
