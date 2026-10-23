"""NexusOS 安装器（幂等）。

rootless 设计：不写 /etc、不建 system unit、不装包。
所有产物落在 $XDG_DATA_HOME/nexus（基底 + 可变层）与 $XDG_CONFIG_HOME/nexus（配置），
systemd 单元装到 ~/.config/systemd/user/（靠 linger 常驻）。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

from . import config, layering, permissions
from .registry import DEFAULT_CONFIG, Registry, discover_from_backend, discover_from_store

HERE = pathlib.Path(__file__).resolve().parent
PKG_ROOT = HERE.parent          # src/nexus -> src
REPO_ROOT = PKG_ROOT.parent      # src -> repo
UNITS_SRC = REPO_ROOT / "systemd"

# 权限策略默认值集中在 permissions 模块（档位推导路径根，不在这里写死）
DEFAULT_POLICY = permissions.default_policy()

DEFAULT_SYSTEM_PROMPT = """你是 NexusOS 的系统代理，运行在一台由 NexusOS 管理的 Linux 工作站上。

你的职责：
1. 先调用工具获取事实，再给结论。绝不凭空描述系统状态。
2. 用户用自然语言描述目标时，你把它翻译成具体工具调用去真正改变系统状态，
   而不是只给操作建议。
3. 涉及破坏性操作时，先说明影响再执行。
4. 回答用简洁的中文，涉及命令时给出可直接复制的形式。

关于被拒绝的操作（重要）：
如果某个工具调用被拒绝（策略限制、只读挂载、沙箱拦截），你必须在最终答复里
**明确写出来**：原请求是什么、被拒绝的原因、你随后改做了什么（或建议用户怎么做）。
绝对不要让用户以为原操作已经完成 —— 尤其不要用"已成功创建"这类措辞
去描述一个实际发生在**别的路径**上的替代操作。

你可以通过 MCP 工具操作这台机器，工具命名空间：
fs.(文件) shell.(命令) git.(版本) sysd.(systemd 单元)
gpu.(显存/模型) journal.(日志) nexus.(系统元数据)
"""


def _write_if_absent(path: pathlib.Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def _render_entry_scripts(base_final: pathlib.Path,
                          data_home: pathlib.Path) -> dict[str, str]:
    """生成入口脚本文本。路径固定指向基底（代码只读）与数据根。"""
    lib = base_final / "lib"
    env = (f'export PYTHONPATH="{lib}"\n'
           f'export NEXUS_DATA_HOME="{data_home}"\n'
           f'export NEXUS_CONFIG_HOME="{config.config_home()}"\n')
    return {
        "nexus": ("#!/bin/sh\n"
                  "# NexusOS 控制面入口（由 nexus-bootstrap 生成）\n"
                  + env + 'exec python3 -m nexus.cli "$@"\n'),
        "nexus-mcp": ("#!/bin/sh\n"
                      "# NexusOS MCP server（stdio, JSON-RPC 2.0）\n"
                      + env + 'exec python3 -m nexus.mcp "$@"\n'),
    }


def _build_payload(dest: pathlib.Path, base_final: pathlib.Path,
                   data_home: pathlib.Path) -> pathlib.Path:
    """构造基底 payload。

    布局：
        lib/nexus/...     Python 包（只读）
        bin/nexus         控制面入口
        bin/nexus-mcp     MCP server 入口
        share/systemd/    systemd --user 单元模板
    """
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(HERE, dest / "lib" / "nexus",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    bindir = dest / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name, content in _render_entry_scripts(base_final, data_home).items():
        p = bindir / name
        p.write_text(content, encoding="utf-8", newline="\n")
        p.chmod(0o755)
    units_dir = dest / "share" / "systemd"
    units_dir.mkdir(parents=True, exist_ok=True)
    if UNITS_SRC.exists():
        for src in UNITS_SRC.glob("nexus-*"):
            shutil.copy2(src, units_dir / src.name)
    # 迁移脚本随基底一起分发 —— 更新时由 nexus update 逐个执行
    mig_dir = dest / "migrations"
    mig_dir.mkdir(parents=True, exist_ok=True)
    mig_src = REPO_ROOT / "migrations"
    if mig_src.exists():
        for src in mig_src.glob("*.sh"):
            shutil.copy2(src, mig_dir / src.name)
    # 版本号随基底分发 —— 安装后的副本也能读到自己的版本
    ver_file = REPO_ROOT / "VERSION"
    if ver_file.exists():
        (dest / "VERSION").write_text(ver_file.read_text(encoding="utf-8").strip() + "\n",
                                      encoding="utf-8", newline="\n")
    return dest


def _install_units(payload: pathlib.Path, data_home: pathlib.Path,
                   base: pathlib.Path) -> list[pathlib.Path]:
    """安装 systemd --user 单元（从 payload 内的 share/systemd 取模板）。"""
    unit_dir = pathlib.Path(os.environ.get(
        "XDG_CONFIG_HOME", str(pathlib.Path.home() / ".config"))) / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    subs = {
        "__NEXUS_DATA__": str(data_home),
        "__BASE_LIB__": str(base / "lib"),
        "__PYTHON__": shutil.which("python3") or "python3",
    }
    installed = []
    units_src = payload / "share" / "systemd"
    if not units_src.exists():
        return installed
    for src in sorted(units_src.glob("nexus-*")):
        text = src.read_text(encoding="utf-8")
        for k, v in subs.items():
            text = text.replace(k, v)
        dest = unit_dir / src.name
        dest.write_text(text, encoding="utf-8", newline="\n")
        installed.append(dest)
    return installed


def _enable_units(installed: list[pathlib.Path]) -> list[dict]:
    out = []
    for unit in installed:
        if unit.suffix != ".service":
            continue
        name = unit.name
        config.run(["systemctl", "--user", "daemon-reload"], timeout=30)
        rc, out_s, err = config.run(["systemctl", "--user", "enable", "--now", name],
                                     timeout=90)
        out.append({"unit": name, "rc": rc, "out": out_s.strip()[:200], "err": err.strip()[:200]})
    return out


def _build_registry(discovered: list[dict] | None = None) -> dict:
    """生成初始注册表。

    种子来自共享 store 的目录扫描（知道有哪些模型可选），
    再叠加后端实时发现（知道谁真的在跑）。
    """
    data = json.loads(json.dumps(DEFAULT_CONFIG))
    reg = Registry(data)
    found: list[dict] = list(discovered or [])
    if not found:
        for backend in reg.backends:
            found += discover_from_backend(backend, reg.backend_url(backend))
    store_found: list[dict] = []
    for backend in reg.backends:
        md = reg.backend(backend).get("models_dir")
        if md:
            store_found += discover_from_store(md)
    merged: dict[str, dict] = {}
    for m in store_found + found:
        name = m["name"]
        if name in merged:
            cur = merged[name]
            cur["vram_mb"] = cur.get("vram_mb") or m.get("vram_mb", 0)
            cur["params"] = cur.get("params") or m.get("params", "")
            cur["quant"] = cur.get("quant") or m.get("quant", "")
            cur["tags"] = sorted(set(cur.get("tags", []) + m.get("tags", [])))
        else:
            merged[name] = dict(m)
    if merged:
        data["model"] = list(merged.values())
        # 默认模型挑显存占用最小的那个 —— 单卡工作站上这是唯一稳妥的默认
        fits = sorted(data["model"], key=lambda m: m.get("vram_mb", 0))
        data["default_model"] = fits[0]["name"] if fits else ""
    return data


def _clean_entry(m: dict) -> dict:
    """去掉空的可选字段，避免配置文件噪音。"""
    out = dict(m)
    for k in ("fallback", "tags", "notes"):
        if k in out and not out[k]:
            out.pop(k)
    return out


def _refresh_registry(quiet: bool = True) -> dict:
    """后端就绪后刷新注册表（把真实模型登记进去，按名字合并）。"""
    reg = Registry()
    found: list[dict] = []
    for backend in reg.backends:
        for m in discover_from_backend(backend, reg.backend_url(backend)):
            m["priority"] = 50
            m["tags"] = ["discovered"]
            found.append(m)
    if not found:
        return {"refreshed": False, "reason": "后端不可达"}

    merged: dict[str, dict] = {}
    for m in reg.models:
        merged[m.name] = _clean_entry(m.to_dict())
    for backend in reg.backends:
        md = reg.backend(backend).get("models_dir")
        if md:
            for m in discover_from_store(md):
                if m["name"] in merged:
                    cur = merged[m["name"]]
                    cur["vram_mb"] = cur.get("vram_mb") or m.get("vram_mb", 0)
                    cur["tags"] = sorted(set(cur.get("tags", []) + m.get("tags", [])))
                else:
                    merged[m["name"]] = _clean_entry(m)
    for m in found:
        name = m["name"]
        if name in merged:
            cur = merged[name]
            # 用户显式覆盖过的字段优先保留，其余以后端实测为准
            cur["vram_mb"] = cur.get("vram_mb") or m.get("vram_mb", 0)
            cur["params"] = cur.get("params") or m.get("params", "")
            cur["quant"] = cur.get("quant") or m.get("quant", "")
            if not cur.get("tags"):
                cur["tags"] = m.get("tags", [])
            if not cur.get("priority"):
                cur["priority"] = m.get("priority", 50)
        else:
            merged[name] = _clean_entry(m)

    data = reg.data
    data["model"] = list(merged.values())
    data["default_model"] = data.get("default_model") or (
        sorted(data["model"], key=lambda m: m.get("vram_mb", 0))[0]["name"]
        if data["model"] else "")
    config.write_toml(config.config_home() / "models.toml", data)
    return {"refreshed": True, "count": len(data["model"]),
            "default": data["default_model"], "models": [m["name"] for m in data["model"]]}


def bootstrap(source: pathlib.Path | None = None, enable: bool = True,
              quiet: bool = False) -> dict:
    """幂等安装。返回安装报告。"""
    report: dict = {"steps": []}
    config.ensure_dirs()
    report["steps"].append({"step": "dirs", "path": str(config.data_home())})

    data_home = config.data_home()
    base = data_home / "base"

    # 1) 配置
    cfg_dir = config.config_home()
    written = []
    if _write_if_absent(cfg_dir / "policy.toml",
                        config.dumps_toml(DEFAULT_POLICY)):
        written.append("policy.toml")
    if _write_if_absent(cfg_dir / "prompts" / "system.md", DEFAULT_SYSTEM_PROMPT):
        written.append("prompts/system.md")
    if _write_if_absent(cfg_dir / "models.toml", config.dumps_toml(_build_registry())):
        written.append("models.toml")
    from .events import DEFAULT_RULES
    if _write_if_absent(cfg_dir / "events.toml",
                        config.dumps_toml({"rule": DEFAULT_RULES})):
        written.append("events.toml")
    report["steps"].append({"step": "config", "written": written})

    # 2) 构造 payload 并铺到基底 / 可变层
    payload = data_home / ".payload"
    if payload.exists():
        shutil.rmtree(payload)
    _build_payload(payload, base, data_home)
    layer = layering.get_backend("symlink")
    try:
        st = layer.install(payload, force=True)
        report["steps"].append({"step": "layer", "status": st})
    except Exception as exc:
        report["steps"].append({"step": "layer", "error": str(exc)})
    shutil.rmtree(payload, ignore_errors=True)

    # 3) PATH 软链
    user_bin = pathlib.Path.home() / ".local" / "bin"
    user_bin.mkdir(parents=True, exist_ok=True)
    linked = []
    for name in ("nexus", "nexus-mcp"):
        target = base / "bin" / name
        link = user_bin / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
        linked.append(str(link))
    report["steps"].append({"step": "path", "linked": linked})

    # 4) systemd --user 单元
    units = _install_units(base, data_home, base)
    report["steps"].append({"step": "units", "installed": [str(u) for u in units]})

    # 5) 启用
    if enable and units:
        report["steps"].append({"step": "enable", "result": _enable_units(units)})
        # 等后端起来再刷新注册表（此刻才拿得到真实模型列表）
        for _ in range(20):
            time.sleep(1)
            res = _refresh_registry()
            if res.get("refreshed"):
                report["steps"].append({"step": "registry", **res})
                break
        else:
            report["steps"].append({"step": "registry", "refreshed": False,
                                    "reason": "后端 20s 内未就绪"})

    if not quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print("\n下一步：直接运行 `nexus` 走一遍新手引导（会真的执行命令并展示输出）。")
    report["version"] = _record_version()
    return report


def _record_version(channel: str | None = None) -> dict:
    """登记已安装版本与通道。"""
    from . import version as version_mod
    ch = channel or os.environ.get("NEXUS_CHANNEL", version_mod.DEFAULT_CHANNEL)
    info = version_mod.record_install(
        version=version_mod.source_version(),
        channel=ch,
        commit=version_mod.source_commit(),
        source="bootstrap")
    return info.to_dict()


if __name__ == "__main__":
    bootstrap()
