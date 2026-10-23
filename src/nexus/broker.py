"""显存仲裁器 —— 系统里唯一有权决定"哪个模型占用 GPU"的角色。

职责：
  1. 读取真实显存状态（nvidia-smi，含 WSL 与 Windows 共享的占用）
  2. 按注册表 + 预算判定模型能否驻留（gpu / partial / cpu 三档）
  3. 强制独占：加载新模型前卸载其他已驻留模型
  4. 给出可执行建议，而不是一句"显存不足"

对应 docs/00-DESIGN-REVIEW.md H2 与 H7。
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import urllib.error
import urllib.request
from typing import Any

from . import audit, config
from .registry import Registry, ModelEntry, current_registry

WSL_NVIDIA_PATH = "/usr/lib/wsl/lib/nvidia-smi"


def nvidia_smi() -> str | None:
    """定位 nvidia-smi（WSL 下不在 PATH 里）。"""
    p = shutil.which("nvidia-smi")
    if p:
        return p
    if os.path.exists(WSL_NVIDIA_PATH):
        return WSL_NVIDIA_PATH
    return None


@dataclasses.dataclass
class GpuState:
    available: bool = False
    name: str = ""
    total_mb: int = 0
    used_mb: int = 0
    free_mb: int = 0
    utilization: int = 0
    power_w: float = 0.0
    temp_c: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def gpu_state() -> GpuState:
    """采集 GPU 状态。注意：WSL 下 used_mb 含 Windows 侧进程占用。"""
    st = GpuState()
    exe = nvidia_smi()
    if not exe:
        st.note = "未找到 nvidia-smi"
        return st
    q = "memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu,name"
    rc, out, err = config.run([exe, f"--query-gpu={q}", "--format=csv,noheader,nounits"], timeout=15)
    if rc != 0 or not out.strip():
        st.note = (err.strip() or "nvidia-smi 查询失败")[:120]
        return st
    line = out.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 6:
        st.note = f"nvidia-smi 输出异常: {line[:120]}"
        return st
    def num(x, d=0):
        try:
            return int(float(x))
        except Exception:
            return d
    st.available = True
    st.used_mb = num(parts[0])
    st.total_mb = num(parts[1])
    st.free_mb = max(0, st.total_mb - st.used_mb)
    st.utilization = num(parts[2])
    try:
        st.power_w = float(parts[3])
    except Exception:
        st.power_w = 0.0
    st.temp_c = num(parts[4])
    st.name = parts[5]
    return st


def _http_json(url: str, payload: dict | None = None, method: str = "GET", timeout: int = 60) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            return e.code, {}
    except Exception as exc:
        return 0, {"error": str(exc)}


class Broker:
    """显存仲裁器。"""

    def __init__(self, registry: Registry | None = None) -> None:
        self._registry = registry

    @property
    def registry(self) -> Registry:
        """按需解析注册表 —— 配置改了立即生效。"""
        return self._registry if self._registry is not None else current_registry()

    # -- 后端状态 -------------------------------------------------------
    def backend_health(self, name: str) -> dict:
        url = self.registry.backend_url(name)
        if not url:
            return {"backend": name, "ok": False, "reason": "未配置 url"}
        code, data = _http_json(f"{url.rstrip('/')}/api/tags", timeout=5)
        if code == 200:
            models = [m.get("name") for m in data.get("models", [])]
            return {"backend": name, "ok": True, "url": url, "models": models}
        # vLLM 用 /v1/models
        code2, data2 = _http_json(f"{url.rstrip('/')}/v1/models", timeout=5)
        if code2 == 200:
            models = [m.get("id") for m in data2.get("data", [])]
            return {"backend": name, "ok": True, "url": url, "models": models}
        return {"backend": name, "ok": False, "url": url,
                "reason": (data.get("error") or "连接失败")[:120]}

    def loaded_models(self, backend: str = "ollama") -> list[dict]:
        """后端当前驻留的模型。"""
        url = self.registry.backend_url(backend)
        if not url:
            return []
        code, data = _http_json(f"{url.rstrip('/')}/api/ps", timeout=5)
        if code != 200:
            return []
        return data.get("models", []) or []

    # -- 驻留状态 -------------------------------------------------------
    def residency(self) -> list[dict]:
        out = []
        for loaded in self.loaded_models("ollama"):
            out.append({
                "name": loaded.get("name"),
                "size_mb": int(loaded.get("size", 0)) // (1024 * 1024),
                "vram_mb": int(loaded.get("size_vram", 0)) // (1024 * 1024),
                "expires_at": loaded.get("expires_at"),
                "backend": "ollama",
            })
        return out

    # -- 适配判定 -------------------------------------------------------
    def fit(self, entry: ModelEntry, gpu: GpuState | None = None) -> dict:
        """判定模型能否驻留显存。

        gpu       —— 全部放显存
        partial   —— 部分放显存，其余卸载到 CPU（Ollama 自动处理）
        cpu       —— 完全放不下，必须 CPU 推理
        """
        gpu = gpu or gpu_state()
        need = int(entry.vram_mb or 0)
        budget = max(0, gpu.total_mb - self.registry.reserve_mb)
        free = gpu.free_mb
        # 该模型若已驻留，它自己的占用不该算作"别人占的" —— 评估重载时需扣除
        own_mb = 0
        try:
            for r in self.residency():
                if r.get("name") == entry.name:
                    own_mb = int(r.get("vram_mb") or r.get("size_mb") or 0)
                    break
        except Exception:
            own_mb = 0
        effective_free = free + own_mb
        if not gpu.available:
            return {"fit": "unknown", "need_mb": need, "free_mb": 0,
                    "budget_mb": 0, "reason": "GPU 不可用"}
        if need == 0:
            return {"fit": "unknown", "need_mb": 0, "free_mb": effective_free,
                    "budget_mb": budget,
                    "reason": "注册表未登记显存开销"}
        if need <= effective_free:
            return {"fit": "gpu", "need_mb": need, "free_mb": effective_free,
                    "budget_mb": budget,
                    "reason": f"显存充足（余 {effective_free}MB > 需 {need}MB）"}
        if need <= budget and need <= self.registry.model_cap_mb:
            return {"fit": "partial", "need_mb": need, "free_mb": effective_free,
                    "budget_mb": budget,
                    "reason": (f"显存不足（可用 {effective_free}MB / 需 {need}MB），"
                               f"将部分层卸载到 CPU，推理变慢但可用")}
        return {"fit": "cpu", "need_mb": need, "free_mb": effective_free,
                "budget_mb": budget,
                "reason": (f"显存严重不足（可用 {effective_free}MB / 需 {need}MB / "
                           f"预算 {budget}MB），只能 CPU 推理")}

    # -- 加载 / 卸载 ---------------------------------------------------
    def unload(self, name: str, backend: str = "ollama") -> dict:
        url = self.registry.backend_url(backend)
        if not url:
            return {"ok": False, "error": "后端未配置"}
        code, data = _http_json(f"{url.rstrip('/')}/api/generate",
                                {"model": name, "keep_alive": 0}, method="POST", timeout=30)
        return {"ok": code == 200, "model": name, "response": data}

    def unload_all(self, backend: str = "ollama") -> list[dict]:
        return [self.unload(m["name"], backend) for m in self.loaded_models(backend) if m.get("name")]

    def load(self, name: str, backend: str = "ollama", force: bool = False) -> dict:
        """加载模型。独占语义：先卸载其他驻留模型。预算不足则拒绝并给建议。"""
        entry = self.registry.get(name)
        if entry is None:
            return {"ok": False, "error": f"注册表中没有模型 {name}"}
        gpu = gpu_state()
        verdict = self.fit(entry, gpu)
        if verdict["fit"] == "unknown" and not force:
            return {"ok": False, "error": verdict.get("reason", "无法判定"), "fit": verdict}
        if verdict["fit"] == "cpu" and not force:
            return {"ok": False, "error": "显存不足，拒绝加载", "fit": verdict,
                    "advice": self.advice(entry, gpu, verdict)}

        # 独占：卸载其他
        evicted = []
        for m in self.loaded_models(backend):
            if m.get("name") and m["name"] != entry.name:
                self.unload(m["name"], backend)
                evicted.append(m["name"])

        url = self.registry.backend_url(backend)
        code, data = _http_json(f"{url.rstrip('/')}/api/generate",
                                {"model": entry.name, "prompt": "", "keep_alive": self.registry.backend(backend).get("keep_alive", "30m")},
                                method="POST", timeout=600)
        ok = code == 200 and "error" not in data
        return {"ok": ok, "model": entry.name, "backend": backend,
                "evicted": evicted, "fit": verdict,
                "error": data.get("error") if not ok else None}

    def advice(self, entry: ModelEntry, gpu: GpuState, verdict: dict) -> list[str]:
        """把"显存不足"翻译成可执行动作。"""
        tips = []
        res = self.residency()
        if res:
            names = ", ".join(f"{r['name']}({r['size_mb']}MB)" for r in res)
            tips.append(f"卸载已驻留模型释放显存: nexus models unload <name>  [当前: {names}]")
        free_models = [m for m in self.registry.models
                       if m.name != entry.name and self.fit(m, gpu)["fit"] == "gpu"]
        if free_models:
            tips.append("改用能装进显存的模型: " +
                        ", ".join(f"{m.name}({m.vram_mb}MB)" for m in free_models))
        if verdict["fit"] == "cpu":
            tips.append("强行加载并接受 CPU 推理: nexus models load "
                        f"{entry.name} --force")
        tips.append(f"释放 Windows 侧 GPU 占用（当前已用 {gpu.used_mb}/{gpu.total_mb}MB，"
                    f"可用 {gpu.free_mb}MB）")
        return tips

    # -- 快照 -----------------------------------------------------------
    def snapshot(self) -> dict:
        gpu = gpu_state()
        entries = []
        for m in self.registry.models:
            v = self.fit(m, gpu)
            entries.append({**m.to_dict(), "fit": v["fit"], "fit_reason": v["reason"]})
        return {
            "gpu": gpu.to_dict(),
            "budget_mb": max(0, gpu.total_mb - self.registry.reserve_mb),
            "reserve_mb": self.registry.reserve_mb,
            "resident": self.residency(),
            "models": entries,
            "backends": [self.backend_health(b) for b in self.registry.backends],
            "ts": __import__("time").time(),
        }

    # -- 模型 stage ------------------------------------------------------
    def stage(self, name: str, source_dir: str | None = None,
              dest_dir: str | None = None) -> dict:
        """把模型从共享 store（drvfs）复制到本地 ext4 store，加速加载。

        背景（实测）：同一份 Q4_K_M 权重，从 /mnt/e（drvfs）加载 125 秒，
        从 ext4 加载 44 秒。drvfs 的 mmap 是瓶颈。stage 是一次性换永久加速。
        """
        import pathlib
        import shutil
        import time as _time

        src_root = pathlib.Path(source_dir or self.registry.backend("ollama")
                                .get("models_dir") or "")
        dst_root = pathlib.Path(dest_dir or config.models_cache_dir())
        if not src_root.exists():
            return {"ok": False, "error": f"源 store 不存在: {src_root}"}
        dst_root.mkdir(parents=True, exist_ok=True)
        (dst_root / "blobs").mkdir(parents=True, exist_ok=True)

        # ollama store 布局：manifests/<repo>/<model>/<tag>
        repo, _, tag = name.partition(":")
        if "/" not in repo:
            repo = f"library/{repo}"
        manifest_rel = f"{repo}/{tag}"
        src_manifest = src_root / "manifests" / manifest_rel
        if not src_manifest.exists():
            # 尝试在 manifests 下模糊查找
            cands = list((src_root / "manifests").rglob(tag))
            if not cands:
                return {"ok": False, "error": f"源 store 中找不到 {name} 的 manifest"}
            src_manifest = cands[0]
            manifest_rel = str(src_manifest.relative_to(src_root / "manifests"))

        import json as _json
        man = _json.loads(src_manifest.read_text(encoding="utf-8"))
        digests = []
        if man.get("config", {}).get("digest"):
            digests.append(man["config"]["digest"])
        for layer in man.get("layers", []):
            if layer.get("digest"):
                digests.append(layer["digest"])

        copied, skipped = [], []
        for dg in digests:
            fname = dg.replace(":", "-")
            s = src_root / "blobs" / fname
            d = dst_root / "blobs" / fname
            if d.exists() and d.stat().st_size == s.stat().st_size:
                skipped.append(fname)
                continue
            if not s.exists():
                return {"ok": False, "error": f"缺少 blob {fname}"}
            shutil.copy2(s, d)
            copied.append(fname)

        dst_manifest = dst_root / "manifests" / manifest_rel
        dst_manifest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_manifest, dst_manifest)
        return {"ok": True, "model": name, "source": str(src_root), "dest": str(dst_root),
                "manifest": str(dst_manifest), "copied": copied, "skipped": skipped,
                "bytes": sum((dst_root / "blobs" / f).stat().st_size for f in set(copied))}


# ---------------------------------------------------------------- 守护


def run_daemon(interval: int = 60) -> None:
    """broker 守护：周期性记录显存快照，并在超预算时自动卸载。"""
    import time
    br = Broker()
    audit.record("broker.daemon.start", interval=interval)
    while True:
        try:
            snap = br.snapshot()
            config.save_state("gpu_snapshot", snap)
            resident = snap.get("resident") or []
            budget = snap.get("budget_mb", 0)
            used = snap.get("gpu", {}).get("used_mb", 0)
            if len(resident) > 1:
                # 独占：只保留一个，卸载其余
                for r in resident[:-1]:
                    br.unload(r["name"])
                    audit.record("broker.auto_unload", model=r["name"], reason="exclusive")
            if used > budget and resident:
                r = resident[0]
                br.unload(r["name"])
                audit.record("broker.auto_unload", model=r["name"],
                            reason="over_budget", used=used, budget=budget)
        except Exception as exc:
            audit.record("broker.daemon_error", error=str(exc))
        time.sleep(interval)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="NexusOS GPU broker")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    a = ap.parse_args()
    if a.daemon:
        run_daemon(a.interval)
    else:
        print(json.dumps(Broker().snapshot(), ensure_ascii=False, indent=2, default=str))

