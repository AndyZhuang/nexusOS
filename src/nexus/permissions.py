"""权限档位 —— 同时驱动沙箱层与工具策略层。

背景（docs/05-PERMISSIONS.md）：
v0.2 里沙箱有 strict/standard/off 三档，但**只有 systemd 层听 tier 的**；
MCP 策略层的 write_roots 是写死的两个目录，跟 tier 完全脱钩。
后果有两个：

  ① 真正生效的边界只有一层 —— local 适配器的工具调用根本没进 systemd 沙箱；
  ② 严格程度和用户想做的事对不上 —— agent 连自己的项目目录都改不了。

本模块把两层的参数收敛到同一个档位上：

    tier ──► systemd 沙箱属性（内核级，ProtectSystem / ProtectHome / ReadWritePaths）
          └─► MCP 工具策略（可写根 / 可读根 / 命令黑名单）

四个档位：

    strict    仅 nexus 数据区可写；系统目录与家目录只读。共享机器 / 多租户用。
    standard  家目录完全可写（含你的项目代码），/tmp 可写，系统目录只读。← 默认
    open      再加 /etc /opt /srv /usr/local 可写。单人独占的工作站用。
    off       不套任何沙箱，可写任意路径。仍留审计记录。

底线：危险命令黑名单（rm -rf /、mkfs、dd 写裸盘……）在**所有档位**都生效，
除非在 policy.toml 里显式写 denied_commands = []。

为什么不用 PrivateDevices=yes：
本机是 GPU 工作站，agent 必须能看到 /dev/dxg 才能查显存（nvidia-smi 依赖它）。
藏掉设备等于让 agent 瞎一半，所以这一项在所有档位都关闭，
代价由其余内核级防护（ProtectSystem/ProtectHome/NoNewPrivileges 等）承担。
"""
from __future__ import annotations

import os
import pathlib
from typing import Any

from . import config

TIERS = ("strict", "standard", "open", "off")
DEFAULT_TIER = "standard"

TIER_LABEL = {
    "strict": "严格",
    "standard": "标准",
    "open": "开放",
    "off": "完全放开",
}

TIER_SUMMARY = {
    "strict": "仅 nexus 数据区可写；系统目录与家目录只读。共享机器或多租户场景用。",
    "standard": "家目录完全可写（你的项目代码就在里面），/tmp 可写；系统目录只读。单人工作站用。",
    "open": "在标准档基础上，再放开 /etc /opt /srv /usr/local。适合把整台机器托��给 agent。",
    "off": "不套任何 systemd 沙箱，可写任意路径。仍会留审计记录。",
}

# 危险命令黑名单 —— 所有档位都生效，除非 policy.toml 显式写 denied_commands = []
BASE_DENIED_COMMANDS = [
    r"\brm\s+(-[a-zA-Z]*\s+)*/(\s|$)",
    r":\(\)\s*\{",
    r"mkfs",
    r"dd\s+if=.*of=/dev/",
    r"shutdown",
    r"reboot",
    r">\s*/dev/sd[a-z]",
    r"wipefs",
]


def home() -> pathlib.Path:
    return pathlib.Path.home()


# ---------------------------------------------------------------- 路径根


def tier_roots(tier: str) -> dict[str, list[str]]:
    """档位对应的可读根 / 可写根。"""
    dh = str(config.data_home())
    ch = str(config.config_home())
    hm = str(home())
    if tier == "strict":
        write = [dh, ch]
    elif tier == "standard":
        write = [hm, dh, ch]
    elif tier == "open":
        write = [hm, "/etc", "/opt", "/srv", "/usr/local", dh, ch]
    else:  # off
        write = ["/"]
    return {"write": write, "read": ["/"]}


def sandbox_properties(tier: str) -> list[str]:
    """档位对应的 systemd 沙箱属性（内核级强制，不可被 agent 绕过）。"""
    if tier == "off":
        return []

    # 所有档位共用的内核防护
    props: list[str] = [
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=no",
        "SystemCallArchitectures=native",
    ]

    if tier == "strict":
        props += ["ProtectSystem=strict", "ProtectHome=read-only"]
        rw = [str(config.data_home()), str(config.config_home())]
    elif tier == "standard":
        props += ["ProtectSystem=strict", "ProtectHome=read-only"]
        # ReadWritePaths 覆盖 ProtectHome=read-only 的只读效果 → 家目录可写
        rw = [str(home()), str(config.data_home()), str(config.config_home())]
    else:  # open
        props += ["ProtectSystem=strict", "ProtectHome=no"]
        rw = [str(home()), "/etc", "/opt", "/srv", "/usr/local",
              str(config.data_home()), str(config.config_home())]

    for p in rw:
        if p and os.path.exists(p):
            props.append(f"ReadWritePaths={p}")

    props.append("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6")
    return props


# ---------------------------------------------------------------- 档位解析


def resolve_tier(policy_data: dict | None = None) -> str:
    """当前生效档位：环境变量 > policy.toml 的 default_tier > 内置默认。"""
    env = (os.environ.get("NEXUS_TIER") or "").strip()
    if env in TIERS:
        return env
    d = policy_data if policy_data is not None else config.load_layered("policy.toml", {})
    t = str(d.get("default_tier") or "").strip()
    if t in TIERS:
        return t
    return DEFAULT_TIER


def policy_for(tier: str | None = None,
               policy_data: dict | None = None) -> dict:
    """把档位解析成一份完整的工具策略。

    policy.toml 里显式写了 read_roots / write_roots / denied_commands 就以它为准
    （用户显式配置优先于档位默认）；没写才用档位的默认值。
    """
    d = dict(policy_data if policy_data is not None else config.load_layered("policy.toml", {}))
    tier = tier or resolve_tier(d)
    if tier not in TIERS:
        tier = DEFAULT_TIER
    roots = tier_roots(tier)

    denied = d.get("denied_commands")
    if denied is None:
        denied = list(BASE_DENIED_COMMANDS)

    return {
        "tier": tier,
        "read_roots": list(d.get("read_roots") or roots["read"]),
        "write_roots": list(d.get("write_roots") or roots["write"]),
        "allow_shell": bool(d.get("allow_shell", True)),
        "allow_restart": bool(d.get("allow_restart", True)),
        "denied_commands": list(denied),
    }


def default_policy(tier: str = DEFAULT_TIER) -> dict:
    """全新安装时写进 policy.toml 的默认策略。

    刻意**不写** read_roots / write_roots —— 那两项由档位推导。
    一旦在这里写死，档位就再也切不动了（policy.toml 的显式值优先于档位默认值）。
    """
    return {
        "default_tier": tier,
        "allow_shell": True,
        "allow_restart": True,
        "denied_commands": list(BASE_DENIED_COMMANDS),
    }


# ---------------------------------------------------------------- 展示与持久化


def describe(tier: str | None = None) -> dict:
    """给 nexus permissions / explain / status 展示当前边界。"""
    t = tier or resolve_tier()
    if t not in TIERS:
        t = DEFAULT_TIER
    pol = policy_for(t)
    return {
        "tier": t,
        "label": TIER_LABEL[t],
        "summary": TIER_SUMMARY[t],
        "read_roots": pol["read_roots"],
        "write_roots": pol["write_roots"],
        "sandbox_properties": sandbox_properties(t),
        "denied_commands": pol["denied_commands"],
        "note": ("危险命令黑名单在所有档位都生效；"
                 "写 denied_commands = [] 可显式清空。"),
    }


def set_tier(tier: str) -> dict:
    """把档位写进用户层 policy.toml（保留其余字段）。

    会清掉 policy.toml 里显式写死的 read_roots / write_roots ——
    否则档位会被那两行架空（显式值优先于档位默认值），切了档没反应。
    """
    if tier not in TIERS:
        raise ValueError(f"未知档位 {tier}（可用: {', '.join(TIERS)}）")
    path = config.config_home() / "policy.toml"
    data = config.read_toml(path) if path.exists() else {}
    if not data:
        data = default_policy(tier)
    cleared = [k for k in ("read_roots", "write_roots") if k in data]
    for k in cleared:
        data.pop(k)
    data["default_tier"] = tier
    config.write_toml(path, data)
    return {"ok": True, "tier": tier, "path": str(path),
            "cleared": cleared,
            "write_roots": policy_for(tier, data)["write_roots"]}
