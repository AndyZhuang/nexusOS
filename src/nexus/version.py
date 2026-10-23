"""版本与发布通道。

学习 Omarchy 的通道模型（stable / rc / edge / dev）：
  stable —— 官方发布版
  rc     —— 大版本前的候选验证版
  edge   —— 最新开发构建
  dev    —— 直接跑本地 git checkout

通道决定 `nexus update` 从哪里取新版本，以及用户愿意承担多少风险。
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import subprocess
import time

from . import config

CHANNELS = ("stable", "rc", "edge", "dev")
DEFAULT_CHANNEL = "stable"

# 各通道的产物来源。{version} 会被替换。
REPO_SLUG = "AndyZhuang/nexusOS"
CHANNEL_SOURCES = {
    "stable": f"https://github.com/{REPO_SLUG}/releases/download/v{{version}}/nexusos-{{version}}.tar.gz",
    "rc": f"https://github.com/{REPO_SLUG}/releases/download/v{{version}}-rc/nexusos-{{version}}-rc.tar.gz",
    "edge": f"https://github.com/{REPO_SLUG}/releases/download/edge/nexusos-edge.tar.gz",
    "dev": "local",
}

PACKAGE_NAME = "nexusos"


def repo_root() -> pathlib.Path | None:
    """从源码树运行时定位仓库根（含 VERSION 文件）。"""
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "VERSION").exists():
            return parent
    return None


def source_version() -> str:
    """版本号。优先取源码树的 VERSION，其次取已安装基底的 VERSION。"""
    root = repo_root()
    if root:
        try:
            return (root / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    installed_version = config.data_home() / "base" / "VERSION"
    if installed_version.exists():
        try:
            return installed_version.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return "0.0.0-dev"


def source_commit() -> str | None:
    root = repo_root()
    if not root:
        return None
    rc, out, _ = config.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], timeout=10)
    return out.strip() if rc == 0 and out.strip() else None


@dataclasses.dataclass
class VersionInfo:
    version: str = "0.0.0"
    channel: str = DEFAULT_CHANNEL
    commit: str | None = None
    installed_at: float = 0.0
    source: str = "unknown"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def installed() -> VersionInfo:
    """读取已安装版本的元信息。"""
    data = config.load_state("version", {})
    return VersionInfo(
        version=data.get("version", "0.0.0"),
        channel=data.get("channel", DEFAULT_CHANNEL),
        commit=data.get("commit"),
        installed_at=data.get("installed_at", 0.0),
        source=data.get("source", "unknown"),
    )


def record_install(version: str, channel: str, commit: str | None = None,
                   source: str = "unknown") -> VersionInfo:
    info = VersionInfo(version=version, channel=channel, commit=commit,
                       installed_at=time.time(), source=source)
    config.save_state("version", info.to_dict())
    return info


def set_channel(channel: str) -> VersionInfo:
    if channel not in CHANNELS:
        raise ValueError(f"未知通道 {channel}（可用: {', '.join(CHANNELS)}）")
    info = installed()
    info.channel = channel
    config.save_state("version", info.to_dict())
    return info


def channel_source(channel: str | None = None) -> str:
    ch = channel or installed().channel
    tmpl = CHANNEL_SOURCES.get(ch, CHANNEL_SOURCES[DEFAULT_CHANNEL])
    if tmpl == "local":
        return "local"
    return tmpl.format(version=installed().version or "0.0.0")


def release_base_url() -> str:
    return os.environ.get("NEXUS_RELEASE_BASE",
                          f"https://github.com/{REPO_SLUG}/releases/download")


def artifact_name(version: str, channel: str = "stable") -> str:
    if channel == "stable":
        return f"{PACKAGE_NAME}-{version}.tar.gz"
    if channel == "rc":
        return f"{PACKAGE_NAME}-{version}-rc.tar.gz"
    return f"{PACKAGE_NAME}-{channel}.tar.gz"


def artifact_url(version: str, channel: str = "stable") -> str:
    base = release_base_url().rstrip("/")
    return f"{base}/v{version}/{artifact_name(version, channel)}" if channel == "stable" \
        else f"{base}/{channel}/{artifact_name(version, channel)}"


def check_version_order(a: str, b: str) -> int:
    """语义化版本比较：a<b 返回 -1。"""
    def key(v: str) -> tuple:
        core = v.split("-")[0]
        pre = v.split("-")[1] if "-" in v else ""
        nums = []
        for part in core.split("."):
            try:
                nums.append(int(part))
            except ValueError:
                nums.append(0)
        while len(nums) < 3:
            nums.append(0)
        # 有预发布标记的版本视为更小
        return (tuple(nums), 0 if pre else 1, pre)
    ka, kb = key(a), key(b)
    return (ka > kb) - (ka < kb)
