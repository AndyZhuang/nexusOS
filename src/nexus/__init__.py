"""NexusOS —— AI 原生系统层（rootless-first）。

设计定稿见 docs/01-NexusOS-Design-v0.2.md
零第三方依赖：仅 Python 3.11+ 标准库（tomllib / http.server / json / subprocess）。
"""

__version__ = "0.2.0"
__all__ = ["config", "layering", "audit", "registry", "broker", "gateway",
           "sandbox", "mcp", "agent", "events", "doctor", "cli"]
