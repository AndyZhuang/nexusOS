"""审计：agent 每次工具调用 / 决策 / 执行结果都落 JSONL。

设计要点：审计是"信任边界的观测面"——即使沙箱被放开，动作仍然留痕。
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import time

from . import config


def _today() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def audit_path() -> pathlib.Path:
    return config.log_dir() / "audit" / f"{_today()}.jsonl"


def record(kind: str, **fields) -> dict:
    """写一条审计记录，返回该记录。"""
    entry = {
        "ts": time.time(),
        "time": _dt.datetime.now().isoformat(timespec="milliseconds"),
        "kind": kind,
    }
    entry.update(fields)
    p = audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_recent(limit: int = 20, kind: str | None = None) -> list[dict]:
    """读最近若干条审计（倒序返回）。"""
    p = audit_path()
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if kind and obj.get("kind") != kind:
            continue
        out.append(obj)
        if len(out) >= limit:
            break
    return out


def count_by_kind() -> dict:
    counts: dict[str, int] = {}
    p = audit_path()
    if not p.exists():
        return counts
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            k = json.loads(line).get("kind", "?")
        except Exception:
            continue
        counts[k] = counts.get(k, 0) + 1
    return counts
