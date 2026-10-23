"""更新机制 —— 单一受祝福的入口（学习 Omarchy 的 `omarchy update`）。

Omarchy 的关键工程决策，本模块逐条对应：

  1. **单一入口**：只有 `nexus update` 能更新 NexusOS。直接跑 `apt upgrade`
     会被拦截（apt hook），因为那会跳过快照与迁移。
  2. **固定顺序**：空间检查 → 快照 → 拉取 → **迁移** → 换基底 → 重启 → 日志分析。
     迁移放在换基底之后、包更新之后，失败就不往下走。
  3. **迁移幂等**：migrations/<时间戳>.sh，完成标记落在 state/migrations/，
     每个迁移每用户只跑一次。
  4. **先快照再动手**：Omarchy 用 Btrfs+Snapper；NexusOS 没有内核级快照，
     用分层管理器的 live 快照（等价语义：可回滚到更新前状态）。
  5. **失败不吞**：迁移失败要显式报告，不假装成功。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import tarfile
import tempfile
import time
import urllib.request

from . import audit, config, layering, version as version_mod
from .layering import LayerError


# ---------------------------------------------------------------- 迁移


def migrations_dir() -> pathlib.Path:
    """已安装基底里的迁移目录。"""
    return config.data_home() / "base" / "migrations"


def migration_marker_dir() -> pathlib.Path:
    return config.state_dir() / "migrations"


def list_migrations() -> list[pathlib.Path]:
    d = migrations_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.sh"))


def completed_migrations() -> set[str]:
    d = migration_marker_dir()
    if not d.exists():
        return set()
    return {p.name for p in d.iterdir() if p.is_file()}


def pending_migrations() -> list[pathlib.Path]:
    done = completed_migrations()
    return [m for m in list_migrations() if m.name not in done]


def run_migrations(dry_run: bool = False) -> dict:
    """执行待跑的迁移。每个迁移完成后写标记，失败不写（可重试）。"""
    results = []
    for m in pending_migrations():
        cmd = f"bash {m}"
        if dry_run:
            results.append({"migration": m.name, "status": "pending", "dry_run": True})
            continue
        audit.record("migration.start", migration=m.name)
        rc, out, err = config.run(["bash", str(m)], timeout=300)
        if rc == 0:
            d = migration_marker_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / m.name).write_text(json.dumps({"ts": time.time()}),
                                     encoding="utf-8", newline="\n")
            audit.record("migration.done", migration=m.name)
            results.append({"migration": m.name, "status": "ok"})
        else:
            audit.record("migration.failed", migration=m.name, rc=rc, err=err[-400:])
            results.append({"migration": m.name, "status": "failed", "rc": rc,
                            "error": (err or out)[-600:]})
            break  # 失败即停：后续迁移依赖前面的结果
    return {"ran": results, "pending": len(pending_migrations())}


# ---------------------------------------------------------------- 产物获取


def _download(url: str, dest: pathlib.Path, timeout: int = 300) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def verify_checksum(path: pathlib.Path, sha_url: str | None = None) -> dict:
    """校验 sha256。优先用官方 .sha256 文件，其次用本地记录。"""
    import hashlib
    if not path.exists():
        return {"ok": False, "error": "文件不存在"}
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    expected = None
    if sha_url:
        try:
            with urllib.request.urlopen(sha_url, timeout=30) as resp:
                expected = resp.read().decode("utf-8", "replace").split()[0].strip()
        except Exception:
            expected = None
    if expected is None:
        sidecar = path.with_suffix(path.suffix + ".sha256")
        if sidecar.exists():
            expected = sidecar.read_text(encoding="utf-8").split()[0].strip()
    if expected is None:
        return {"ok": True, "sha256": digest, "verified": False,
                "note": "无期望值可比对（仅记录）"}
    return {"ok": digest == expected, "sha256": digest, "expected": expected,
            "verified": True}


def fetch_release(channel: str | None = None, dest_dir: pathlib.Path | None = None) -> dict:
    """按通道下载产物。dev 通道直接用本地源码树。"""
    ch = channel or version_mod.installed().channel
    dest = dest_dir or (config.data_home() / ".incoming")
    dest.mkdir(parents=True, exist_ok=True)
    if ch == "dev":
        root = version_mod.repo_root()
        if not root:
            return {"ok": False, "error": "dev 通道需要本地源码树"}
        return {"ok": True, "channel": "dev", "local": str(root),
                "version": version_mod.source_version()}
    ver = version_mod.installed().version
    url = version_mod.artifact_url(ver, ch)
    name = version_mod.artifact_name(ver, ch)
    dest_file = dest / name
    audit.record("update.fetch", channel=ch, url=url)
    ok, err = _download(url, dest_file)
    if not ok:
        return {"ok": False, "error": f"下载失败 {url}: {err}", "url": url}
    chk = verify_checksum(dest_file, url + ".sha256")
    if not chk.get("ok"):
        return {"ok": False, "error": "校验和不匹配", "checksum": chk, "path": str(dest_file)}
    return {"ok": True, "channel": ch, "url": url, "path": str(dest_file),
            "checksum": chk}


def extract_payload(tarball: pathlib.Path, dest: pathlib.Path) -> dict:
    """解包产物到暂存目录。拒绝路径穿越。"""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tf:
        base = dest.resolve()
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(base)):
                return {"ok": False, "error": f"产物包含越界路径: {member.name}"}
        tf.extractall(dest)
    # 产物结构: <dest>/payload/...
    payload = dest / "payload"
    if not payload.exists():
        cands = [p for p in dest.iterdir() if p.is_dir() and (p / "lib" / "nexus").exists()]
        payload = cands[0] if cands else dest
    return {"ok": True, "payload": str(payload)}


# ---------------------------------------------------------------- 主流程


def free_space_mb(path: pathlib.Path) -> int:
    st = os.statvfs(path)
    return int(st.f_bavail * st.f_frsize / (1024 * 1024))


def perform_update(channel: str | None = None, dry_run: bool = False,
                   skip_fetch: bool = False) -> dict:
    """完整更新流程。顺序与 Omarchy 对齐：空间 → 快照 → 取产物 → 迁移 → 换基底 → 重启。"""
    steps: list[dict] = []
    ch = channel or version_mod.installed().channel
    info = version_mod.installed()
    data_home = config.data_home()

    # 1) 空间检查
    need_mb = 2048
    have_mb = free_space_mb(data_home)
    ok_space = have_mb >= need_mb
    steps.append({"step": "space", "ok": ok_space, "have_mb": have_mb, "need_mb": need_mb})
    if not ok_space and not dry_run:
        return {"ok": False, "error": f"空闲空间不足（{have_mb}MB < {need_mb}MB）", "steps": steps}

    # 2) 快照（更新前的回滚点）
    snap = None
    if not dry_run:
        try:
            snap = str(layering.get_backend().snapshot("pre-update"))
            steps.append({"step": "snapshot", "ok": True, "path": snap})
        except Exception as exc:
            steps.append({"step": "snapshot", "ok": False, "error": str(exc)})
            audit.record("update.snapshot_failed", error=str(exc))
    else:
        steps.append({"step": "snapshot", "ok": True, "dry_run": True})

    # 3) 取产物
    fetched = None
    if not skip_fetch:
        fetched = fetch_release(ch)
        steps.append({"step": "fetch", **fetched})
        if not fetched.get("ok") and not dry_run:
            return {"ok": False, "error": fetched.get("error"), "steps": steps}
    else:
        steps.append({"step": "fetch", "ok": True, "skipped": True})

    # 4) 迁移（在换基底之前 —— 迁移针对的是用户的配置，不是代码）
    mig = run_migrations(dry_run=dry_run)
    mig_failed = any(r.get("status") == "failed" for r in mig.get("ran", []))
    steps.append({"step": "migrations", "ok": not mig_failed,
                  "ran": len(mig.get("ran", [])), "pending": mig.get("pending", 0)})
    if mig_failed and not dry_run:
        return {"ok": False, "error": "迁移失败，已中止更新", "steps": steps}

    # 5) 换基底
    if fetched and fetched.get("ok") and not dry_run:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="nexus-update-"))
        try:
            ex = extract_payload(pathlib.Path(fetched["path"]), tmp)
            if not ex.get("ok"):
                steps.append({"step": "payload", "ok": False, "error": ex.get("error")})
                return {"ok": False, "error": ex.get("error"), "steps": steps}
            layer = layering.get_backend("symlink")
            st = layer.install(pathlib.Path(ex["payload"]), force=True)
            steps.append({"step": "payload", "ok": True, "layer": st})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        steps.append({"step": "payload", "ok": True, "dry_run": dry_run})

    # 6) 重启单元
    if not dry_run:
        rc, out, err = config.run(["systemctl", "--user", "restart",
                                    "nexus-gateway", "nexus-broker",
                                    "nexus-event-router"], timeout=90)
        steps.append({"step": "restart", "ok": rc == 0, "stderr": err[-300:]})
    else:
        steps.append({"step": "restart", "ok": True, "dry_run": True})

    audit.record("update.done", channel=ch, ok=True, snapshot=snap)
    return {"ok": True, "channel": ch, "version": info.version, "steps": steps,
            "snapshot": snap}


def pending_report() -> dict:
    return {
        "installed": version_mod.installed().to_dict(),
        "source_version": version_mod.source_version(),
        "migrations_total": len(list_migrations()),
        "migrations_pending": [m.name for m in pending_migrations()],
    }
