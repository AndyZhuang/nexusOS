"""配置层：路径解析、两级配置合并、TOML 读写。

设计约束（见 docs/01-NexusOS-Design-v0.2.md §4）：
- rootless-first：所有可写路径都在 $XDG 之下；/etc/nexus 仅作为只读系统层被合并进来。
- 零第三方依赖：TOML 解析用标准库 tomllib；写入用本模块自带的最小序列化器。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore

HOME = pathlib.Path.home()

# ---------------------------------------------------------------- 路径


def data_home() -> pathlib.Path:
    """持久数据根（NEXUS_DATA_HOME > XDG_DATA_HOME/nexus > ~/.local/share/nexus）。"""
    env = os.environ.get("NEXUS_DATA_HOME")
    if env:
        return pathlib.Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = pathlib.Path(xdg).expanduser() if xdg else HOME / ".local" / "share"
    return base / "nexus"


def config_home() -> pathlib.Path:
    """用户配置根（NEXUS_CONFIG_HOME > XDG_CONFIG_HOME/nexus > ~/.config/nexus）。"""
    env = os.environ.get("NEXUS_CONFIG_HOME")
    if env:
        return pathlib.Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(xdg).expanduser() if xdg else HOME / ".config"
    return base / "nexus"


def system_config_home() -> pathlib.Path:
    """系统层配置根，只读。存在才参与合并。"""
    return pathlib.Path(os.environ.get("NEXUS_SYSTEM_CONFIG", "/etc/nexus"))


# 分层相关路径（rootless profile）
def base_dir() -> pathlib.Path:
    return data_home() / "base"


def live_dir() -> pathlib.Path:
    return data_home() / "live"


def state_dir() -> pathlib.Path:
    return data_home() / "state"


def log_dir() -> pathlib.Path:
    return data_home() / "log"


def incidents_dir() -> pathlib.Path:
    return data_home() / "incidents"


def sessions_dir() -> pathlib.Path:
    return data_home() / "agent" / "sessions"


def memory_dir() -> pathlib.Path:
    return data_home() / "agent" / "memory"


def models_cache_dir() -> pathlib.Path:
    return data_home() / "ollama-models"


def all_dirs() -> list[pathlib.Path]:
    return [
        data_home(), config_home(), base_dir(), live_dir(), state_dir(),
        log_dir(), incidents_dir(), sessions_dir(), memory_dir(),
        log_dir() / "audit", log_dir() / "events", log_dir() / "gateway",
        config_home() / "prompts",
    ]


def ensure_dirs() -> None:
    for d in all_dirs():
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- TOML 写


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    s = str(v)
    return json.dumps(s, ensure_ascii=False)


def dumps_toml(data: dict) -> str:
    """最小 TOML 序列化器：支持嵌套表、表数组、标量、标量数组。

    只覆盖本项目配置文件的实际形态，不追求完整 TOML 规范。
    """
    out: list[str] = []

    def emit_table(prefix: str, tbl: dict) -> None:
        # 值分三类：dict → 子表；list[dict] → 表数组；其余 → 标量/内联数组
        scalars, arrays, tables = {}, {}, {}
        for k, v in tbl.items():
            if isinstance(v, dict):
                tables[k] = v
            elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                arrays[k] = v
            else:
                scalars[k] = v
        for k, v in scalars.items():
            out.append(f"{k} = {_fmt_value(v)}")
        for k, v in arrays.items():
            out.append("")
            for item in v:
                out.append(f"[[{prefix}{k}]]")
                emit_table(f"{prefix}{k}.", item)
        for k, v in tables.items():
            name = f"{prefix}{k}"
            # 空的父表不再单独输出表头，避免 [backends] 这种噪音
            if not any(x for x in v.values()):
                continue
            out.append("")
            out.append(f"[{name}]")
            emit_table(f"{name}.", v)

    emit_table("", data)
    text = "\n".join(out).strip() + "\n"
    return text


def write_toml(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dumps_toml(data), encoding="utf-8", newline="\n")
    tmp.replace(path)


def read_toml(path: pathlib.Path) -> dict:
    if not path.exists() or tomllib is None:
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


# ---------------------------------------------------------------- 配置合并


def _deep_merge(base: dict, over: dict) -> dict:
    """逐键覆盖；嵌套 dict 递归；列表按 name 字段合并（否则整体替换）。"""
    res = dict(base)
    for k, v in over.items():
        if k in res and isinstance(res[k], dict) and isinstance(v, dict):
            res[k] = _deep_merge(res[k], v)
        elif k in res and isinstance(res[k], list) and isinstance(v, list):
            res[k] = _merge_named_list(res[k], v)
        else:
            res[k] = v
    return res


def _merge_named_list(old: list, new: list) -> list:
    """列表元素带 name 字段时按键合并，否则整体替换。"""
    if not all(isinstance(x, dict) and "name" in x for x in old + new):
        return new
    by_name = {x["name"]: x for x in old}
    for x in new:
        if x["name"] in by_name:
            by_name[x["name"]] = _deep_merge(by_name[x["name"]], x)
        else:
            by_name[x["name"]] = x
    return list(by_name.values())


def load_layered(name: str, defaults: dict | None = None) -> dict:
    """读取配置：系统层 → 用户层 逐键覆盖。

    系统层来自 /etc/nexus（只读，存在才读）；用户层来自 $XDG_CONFIG_HOME/nexus。
    """
    data = dict(defaults or {})
    for root in (system_config_home(), config_home()):
        f = root / name
        if f.exists():
            data = _deep_merge(data, read_toml(f))
    return data


def config_sources(name: str) -> list[dict]:
    """列出配置来源及其是否存在（供 nexus status 展示）。"""
    out = []
    for root in (system_config_home(), config_home()):
        f = root / name
        out.append({"path": str(f), "exists": f.exists(), "scope": "system" if root == system_config_home() else "user"})
    return out


# ---------------------------------------------------------------- 运行时状态


def save_state(name: str, payload: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    p = state_dir() / f"{name}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def load_state(name: str, default: dict | None = None) -> dict:
    p = state_dir() / f"{name}.json"
    if not p.exists():
        return dict(default or {})
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return dict(default or {})


# ---------------------------------------------------------------- 外部命令


def run(cmd: list[str], timeout: int = 30, env: dict | None = None,
        stdin_data: str | None = None) -> tuple[int, str, str]:
    """执行外部命令，返回 (rc, stdout, stderr)。不抛异常。"""
    try:
        e = dict(os.environ)
        if env:
            e.update(env)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=e, input=stdin_data)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except Exception as exc:  # pragma: no cover
        return 1, "", str(exc)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"
