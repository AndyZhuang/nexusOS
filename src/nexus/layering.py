"""分层管理器：可变层 + 只读基底 + 原子回厂。

目录语义（rootless profile）：
    $NEXUS_DATA/base/      只读基底 —— bootstrap 安装的 payload 快照
    $NEXUS_DATA/live/      可变层实体 —— 开发者的改动落在这里
    $NEXUS_DATA/current -> live   原子可换的符号链接（回厂时整体替换）

两个后端，同一套接口：
    symlink   —— rootless 默认。不需要 root，不动 mount namespace。
    overlayfs —— root profile 可选。内核 overlay，reset = 清空 upperdir。

与 v0.1 的关键差别：upper 落盘而非 tmpfs（见 docs/00-DESIGN-REVIEW.md H1）。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import time

from . import config


class LayerError(RuntimeError):
    pass


class SymlinkBackend:
    """rootless 后端：基底 + 可变层，符号链接原子切换。"""

    name = "symlink"

    def __init__(self) -> None:
        self.root = config.data_home()
        self.base = self.root / "base"
        self.live = self.root / "live"
        self.link = self.root / "current"      # -> live
        self.staging = self.root / ".staging"

    # -- 状态 ---------------------------------------------------------
    def status(self) -> dict:
        target = os.readlink(self.link) if self.link.is_symlink() else None
        return {
            "backend": self.name,
            "root": str(self.root),
            "base": str(self.base),
            "base_exists": self.base.exists(),
            "base_entries": sorted(p.name for p in self.base.iterdir())[:20] if self.base.exists() else [],
            "live": str(self.live),
            "live_exists": self.live.exists(),
            "current": str(self.link),
            "current_target": target,
            "current_ok": target is not None and pathlib.Path(target).exists(),
        }

    # -- 安装 ---------------------------------------------------------
    def install(self, payload: pathlib.Path, force: bool = False) -> dict:
        """把 payload 物化成基底，并铺一份到可变层。"""
        self.root.mkdir(parents=True, exist_ok=True)
        if force and self.base.exists():
            shutil.rmtree(self.base)
        if not self.base.exists() or not any(self.base.iterdir()):
            shutil.copytree(payload, self.base, symlinks=True)
        if not self.live.exists():
            shutil.copytree(self.base, self.live, symlinks=True)
        self._relink(self.live)
        return self.status()

    def _relink(self, target: pathlib.Path) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / ".current.new"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(target)
        os.replace(tmp, self.link)

    # -- 回厂 ---------------------------------------------------------
    def reset(self, dry_run: bool = False) -> dict:
        st = self.status()
        if not st["base_exists"]:
            raise LayerError("基底不存在，先执行 nexus install")
        if dry_run:
            return {"backend": self.name, "dry_run": True,
                    "would_rebuild": str(self.live),
                    "would_restore_from": str(self.base),
                    "current": st["current_target"]}
        staged = self.staging / f"live-{int(time.time())}"
        if staged.exists():
            shutil.rmtree(staged)
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.base, staged, symlinks=True)
        old = self.live
        newlive = self.root / f"live.{int(time.time())}"
        os.replace(staged, newlive)
        self._relink(newlive)
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        return self.status()

    # -- 快照 ---------------------------------------------------------
    def snapshot(self, name: str) -> pathlib.Path:
        snaps = self.root / "snapshots"
        snaps.mkdir(parents=True, exist_ok=True)
        dest = snaps / f"{name}-{int(time.time())}"
        shutil.copytree(self.live, dest, symlinks=True)
        (dest / ".nexus-snapshot").write_text(
            json.dumps({"name": name, "ts": time.time()}), encoding="utf-8")
        return dest

    def restore(self, name: str) -> dict:
        snaps = self.root / "snapshots"
        cands = sorted(snaps.glob(f"{name}-*"))
        if not cands:
            raise LayerError(f"找不到快照: {name}")
        return self._swap_in(cands[-1])

    def _swap_in(self, src: pathlib.Path) -> dict:
        staged = self.staging / f"restore-{int(time.time())}"
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, staged, symlinks=True)
        old = self.live
        newlive = self.root / f"live.{int(time.time())}"
        os.replace(staged, newlive)
        self._relink(newlive)
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        return self.status()


class OverlayBackend:
    """root profile 后端：内核 overlayfs，upperdir 落盘。"""

    name = "overlayfs"

    def __init__(self) -> None:
        self.root = config.data_home()
        self.base = self.root / "base"
        self.upper = self.root / "upper"
        self.work = self.root / "work"
        self.mount = self.root / "current"

    def status(self) -> dict:
        return {
            "backend": self.name,
            "root": str(self.root),
            "base": str(self.base),
            "base_exists": self.base.exists(),
            "upper": str(self.upper),
            "mounted": os.path.ismount(self.mount),
            "live": str(self.mount),
        }

    def install(self, payload: pathlib.Path, force: bool = False) -> dict:
        if force and self.base.exists():
            shutil.rmtree(self.base)
        shutil.copytree(payload, self.base, symlinks=True)
        self.upper.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.mount.parent.mkdir(parents=True, exist_ok=True)
        self.mount.mkdir(parents=True, exist_ok=True)
        rc, _, err = config.run(["mount", "-t", "overlay", "overlay",
                                  f"-o lowerdir={self.base},upperdir={self.upper},workdir={self.work}",
                                  str(self.mount)])
        if rc != 0:
            raise LayerError(f"overlay mount 失败（需要 root）: {err.strip()}")
        return self.status()

    def reset(self, dry_run: bool = False) -> dict:
        if dry_run:
            return {"backend": self.name, "dry_run": True, "would_clear": str(self.upper)}
        if self.upper.exists():
            for child in self.upper.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        return self.status()

    def snapshot(self, name: str) -> pathlib.Path:
        snaps = self.root / "snapshots"
        snaps.mkdir(parents=True, exist_ok=True)
        dest = snaps / f"{name}-{int(time.time())}"
        shutil.copytree(self.upper, dest, symlinks=True)
        return dest

    def restore(self, name: str) -> dict:
        snaps = self.root / "snapshots"
        cands = sorted(snaps.glob(f"{name}-*"))
        if not cands:
            raise LayerError(f"找不到快照: {name}")
        src = cands[-1]
        if self.upper.exists():
            for child in self.upper.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        shutil.copytree(src, self.upper, symlinks=True)
        return self.status()


def get_backend(prefer: str | None = None):
    """选择后端。prefer ∈ {symlink, overlayfs}；默认 symlink（rootless）。"""
    if prefer == "overlayfs":
        return OverlayBackend()
    if prefer == "symlink":
        return SymlinkBackend()
    return SymlinkBackend()


def live_path() -> pathlib.Path:
    """当前生效的可变层路径（跟随 current 链接）。"""
    link = config.data_home() / "current"
    if link.is_symlink():
        return pathlib.Path(os.readlink(link))
    return config.data_home() / "live"
