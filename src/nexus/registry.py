"""模型注册表 —— 全系统唯一真相源。

为什么必须有它（见 docs/00-DESIGN-REVIEW.md H2）：
v0.1 的"双后端共存"会让同一个模型在 Ollama 和 vLLM 各加载一份权重，显存翻倍。
注册表把"模型 → 后端 → 显存开销 → 归属"钉死在一处，仲裁器据此做**独占加载**。
"""
from __future__ import annotations

import dataclasses
from typing import Any

from . import config

DEFAULT_CONFIG: dict[str, Any] = {
    "backends": {
        "ollama": {
            "url": "http://127.0.0.1:11435",
            "kind": "local",
            "models_dir": "/mnt/e/Ollama/models",
            "keep_alive": "30m",
        },
        "vllm": {
            "url": "http://127.0.0.1:8000",
            "kind": "remote",
            "enabled": False,
        },
    },
    "gpu": {
        # 预留给系统与其他进程的显存，不分配给模型
        "reserve_mb": 1024,
        # 单模型最大显存上限（超过则判定为必须部分卸载到 CPU）
        "model_cap_mb": 12288,
    },
    "default_model": "dolphin3:8b",
    "model": [],
}


@dataclasses.dataclass
class ModelEntry:
    name: str
    backend: str = "ollama"
    vram_mb: int = 0
    params: str = ""
    quant: str = ""
    priority: int = 100
    tags: list[str] = dataclasses.field(default_factory=list)
    fallback: list[str] = dataclasses.field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ModelEntry":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class Registry:
    """模型注册表。系统层 /etc/nexus/models.toml 与用户层合并。"""

    def __init__(self, data: dict | None = None) -> None:
        self.data = data if data is not None else config.load_layered("models.toml", DEFAULT_CONFIG)
        self.data.setdefault("backends", DEFAULT_CONFIG["backends"])
        self.data.setdefault("gpu", DEFAULT_CONFIG["gpu"])

    # -- 后端 ---------------------------------------------------------
    @property
    def backends(self) -> dict:
        return self.data.get("backends", {})

    def backend(self, name: str) -> dict:
        return self.backends.get(name, {})

    def backend_url(self, name: str) -> str:
        return self.backend(name).get("url", "")

    # -- 模型 ---------------------------------------------------------
    @property
    def models(self) -> list[ModelEntry]:
        return [ModelEntry.from_dict(m) for m in self.data.get("model", [])]

    def get(self, name: str) -> ModelEntry | None:
        for m in self.models:
            if m.name == name:
                return m
        # 允许 "8b" 这类短名匹配
        hits = [m for m in self.models if m.name.split(":")[-1] == name]
        return hits[0] if len(hits) == 1 else None

    @property
    def default_model(self) -> str:
        return self.data.get("default_model", "")

    def resolve(self, name: str) -> ModelEntry | None:
        """解析请求里的模型名：精确 → 短名 → 默认 → 按 tag 选最高优先级。"""
        if name:
            m = self.get(name)
            if m:
                return m
        if self.default_model:
            m = self.get(self.default_model)
            if m:
                return m
        tagged = [m for m in self.models if "chat" in m.tags]
        pool = tagged or self.models
        return min(pool, key=lambda m: m.priority) if pool else None

    # -- GPU 策略 ------------------------------------------------------
    @property
    def gpu_cfg(self) -> dict:
        return self.data.get("gpu", {})

    @property
    def reserve_mb(self) -> int:
        return int(self.gpu_cfg.get("reserve_mb", 1024))

    @property
    def model_cap_mb(self) -> int:
        return int(self.gpu_cfg.get("model_cap_mb", 12288))

    # -- 持久化 --------------------------------------------------------
    def save_user_overlay(self, path=None) -> None:
        """把当前注册表写到用户层（供 nexus models set 之类命令用）。"""
        p = path or (config.config_home() / "models.toml")
        config.write_toml(p, self.data)

    def summary(self) -> dict:
        return {
            "backends": {k: {"url": v.get("url"), "kind": v.get("kind"), "enabled": v.get("enabled", True)}
                         for k, v in self.backends.items()},
            "model_count": len(self.models),
            "default_model": self.default_model,
            "reserve_mb": self.reserve_mb,
        }


_CACHE: dict = {"registry": None, "mtime": None}


def current_registry() -> "Registry":
    """热重载的注册表 —— 配置改了不必重启常驻服务。"""
    p = config.config_home() / "models.toml"
    try:
        mt = p.stat().st_mtime
    except OSError:
        mt = None
    if _CACHE["registry"] is None or mt != _CACHE["mtime"]:
        _CACHE["registry"] = Registry()
        _CACHE["mtime"] = mt
    return _CACHE["registry"]


def discover_from_store(models_dir: str | None = None) -> list[dict]:
    """扫描 ollama store 目录，列出全部可用模型（不加载权重）。

    用途：注册表必须知道"有哪些模型可选"，而不是只知道"后端当前挂着谁"。
    这也是显存仲裁的输入 —— 17.8GB 的模型必须提前判定为不可驻留。
    """
    import json
    import pathlib
    if not models_dir:
        return []
    root = pathlib.Path(models_dir)
    mdir = root / "manifests"
    if not mdir.exists():
        return []
    out = []
    for man in sorted(mdir.rglob("*")):
        if not man.is_file():
            continue
        rel = man.relative_to(mdir).as_posix()
        if rel.startswith("registry.ollama.ai/"):
            rel = rel[len("registry.ollama.ai/"):]
        parts = rel.split("/")
        if len(parts) < 2:
            continue
        repo = "/".join(parts[:-1])
        tag = parts[-1]
        if repo.startswith("library/"):
            repo = repo[len("library/"):]
        name = f"{repo}:{tag}" if repo else tag
        try:
            data = json.loads(man.read_text(encoding="utf-8"))
        except Exception:
            continue
        total = 0
        for layer in data.get("layers", []):
            if layer.get("mediaType") != "application/vnd.ollama.image.model":
                continue
            blob = root / "blobs" / layer["digest"].replace(":", "-")
            try:
                total += blob.stat().st_size
            except OSError:
                pass
        if total == 0:
            continue
        out.append({
            "name": name,
            "backend": "ollama",
            "vram_mb": total // (1024 * 1024),
            "params": "",
            "quant": "",
            "priority": 60,
            "tags": ["store"],
            "source": str(models_dir),
        })
    return out


def discover_from_backend(backend: str, url: str) -> list[dict]:
    """从后端实时发现模型（用于把后端已有模型登记进注册表）。"""
    import json
    import urllib.request
    if not url:
        return []
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    out = []
    for m in data.get("models", []):
        det = m.get("details", {}) or {}
        out.append({
            "name": m.get("name"),
            "backend": backend,
            "vram_mb": int(m.get("size", 0)) // (1024 * 1024),
            "params": det.get("parameter_size", ""),
            "quant": det.get("quantization_level", ""),
            "priority": 50,
            "tags": ["discovered"],
        })
    return out
