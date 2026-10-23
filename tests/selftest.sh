#!/usr/bin/env bash
# NexusOS 自检 —— 一条命令验证整条链路。
#
#   bash tests/selftest.sh
#
# 全部用确定性断言（mock 适配器），不需要模型加载，秒级完成。
# 真实 LLM 链路的测试见 tests/selftest-llm.sh。
set -uo pipefail

export PATH="$HOME/.local/bin:$PATH"
PASS=0; FAIL=0

# 仓库根（从脚本位置推导，不写死绝对路径）
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export NEXUS_REPO="$REPO"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
sec()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- Layer 0
sec "Layer 0 · 基座"
if nexus status > /dev/null 2>&1; then ok "nexus status 可用"; else bad "nexus status 失败"; fi
if nexus reset --dry-run 2>&1 | grep -q "would_rebuild"; then ok "分层 reset --dry-run 可用"; else bad "分层 reset 异常"; fi
if systemctl --user list-unit-files 'nexus-*' 2>/dev/null | grep -q nexus-gateway; then
  ok "systemd --user 单元已注册"; else bad "systemd 单元缺失"; fi
if systemctl --user is-active nexus-gateway > /dev/null 2>&1; then
  ok "nexus-gateway 运行中"; else bad "nexus-gateway 未运行"; fi

# ---------------------------------------------------------------- Layer 1
sec "Layer 1 · 资源仲裁"
if nexus models list 2>&1 | grep -q "模型"; then ok "模型注册表可读"; else bad "模型注册表异常"; fi
if nexus gpu 2>&1 | grep -q "显存"; then ok "显存快照可读"; else bad "显存快照异常"; fi
# 适配判定是纯函数，用**注入的**显存状态做确定性断言（不受本机负载影响）
FIT=$(python3 - <<'PY'
import os
import sys
sys.path.insert(0, os.environ["NEXUS_REPO"] + "/src")
from nexus.broker import Broker, GpuState
from nexus.registry import ModelEntry

class FakeBroker(Broker):
    def __init__(self, gpu):
        super().__init__()
        self._gpu = gpu
    def residency(self, backend="ollama"):
        return []            # 隔离本机真实驻留状态
    def fit(self, entry, gpu=None):
        return Broker.fit(self, entry, self._gpu)

m = ModelEntry(name="qwen3.6:27b", vram_mb=16057)   # 16057 > 预算 15360
busy = FakeBroker(GpuState(available=True, name="t", total_mb=16384, used_mb=13500, free_mb=2884))
idle = FakeBroker(GpuState(available=True, name="t", total_mb=16384, used_mb=0, free_mb=16384))
# 显存被占满 → cpu
assert busy.fit(m)["fit"] == "cpu", busy.fit(m)
# 显存全空但仍超出"扣掉预留后的预算" → 也不能判 gpu（必须守住预留量）
assert idle.fit(m)["fit"] != "gpu", idle.fit(m)
# 8B 在两种状态下都能完全驻留
m8 = ModelEntry(name="dolphin3:8b", vram_mb=4692)
assert busy.fit(m8)["fit"] == "partial", busy.fit(m8)
assert idle.fit(m8)["fit"] == "gpu", idle.fit(m8)
print("cpu")
PY
)
if [ "$FIT" = "cpu" ]; then
  ok "27B 在显存紧张时判为 cpu（拒绝驻留）"
else
  bad "27B 适配判定错误: '$FIT'（期望 cpu）"
fi
if timeout 10 curl -sf http://127.0.0.1:8080/v1/models > /dev/null; then
  ok "网关 /v1/models 可用"; else bad "网关 /v1/models 不可用"; fi
if timeout 10 curl -sf http://127.0.0.1:8080/healthz > /dev/null; then
  ok "网关 /healthz 可用"; else bad "网关 /healthz 不可用"; fi

# ---------------------------------------------------------------- Layer 5
sec "Layer 5 · MCP"
TOOLS=$(nexus mcp list 2>/dev/null | tail -1 | grep -oE '[0-9]+')
if [ "${TOOLS:-0}" -ge 20 ]; then ok "MCP 工具 $TOOLS 个"; else bad "MCP 工具过少: ${TOOLS:-0}"; fi
if nexus mcp test 2>&1 | grep -q "protocolVersion"; then ok "MCP initialize 握手成功"; else bad "MCP 握手失败"; fi
if nexus mcp test 2>&1 | grep -q "策略拒绝写入"; then
  ok "工具层策略边界生效（拒绝写 /etc）"; else bad "策略边界未生效"; fi

# ---------------------------------------------------------------- Layer 2
sec "Layer 2 · Agent 宿主（mock 适配器，确定性）"
OUT=$(nexus agent run --adapter mock "检查系统" 2>&1)
if echo "$OUT" | grep -q "系统可观测性链路正常"; then ok "mock agent 完成工具调用"; else bad "mock agent 异常"; fi
if echo "$OUT" | grep -q "gpu.snapshot"; then ok "工具调用链贯通"; else bad "工具调用未发生"; fi

sec "信任边界"
if nexus explain --tier strict 2>&1 | grep -q "ProtectSystem=strict"; then
  ok "strict tier 沙箱属性正确"; else bad "沙箱属性缺失"; fi
if nexus explain --tier strict 2>&1 | grep -q "ReadWritePaths"; then
  ok "数据区可写白名单已配置"; else bad "可写白名单缺失"; fi

# ---------------------------------------------------------------- 权限档位
sec "权限档位（0.3.0）"
if nexus permissions 2>&1 | grep -q "权限档位"; then
  ok "nexus permissions 可用"; else bad "nexus permissions 异常"; fi
if nexus permissions list 2>&1 | grep -qE "strict|standard|open|off"; then
  ok "四个档位可列出"; else bad "档位列表异常"; fi

# 档位 → 路径根的映射是纯函数，用断言验证（不依赖本机状态）
PERM=$(python3 - <<'PY'
import os
import pathlib
import sys
sys.path.insert(0, os.environ["NEXUS_REPO"] + "/src")
from nexus import permissions

home = str(pathlib.Path.home())
errs = []

def has_home(tier):
    for r in permissions.policy_for(tier)["write_roots"]:
        if r == "/" or r == home or home in pathlib.Path(r).parents:
            return True
    return False

# strict 不含家目录，standard / open 含家目录
if has_home("strict"):
    errs.append("strict 档不应含家目录")
if not has_home("standard"):
    errs.append("standard 档应含家目录")
if not has_home("open"):
    errs.append("open 档应含家目录")
# off 全放开
if "/" not in permissions.policy_for("off")["write_roots"]:
    errs.append("off 档应可写 /")
# 危险命令黑名单在所有档位都存在
for tier in permissions.TIERS:
    if not permissions.policy_for(tier)["denied_commands"]:
        errs.append(f"{tier} 档黑名单为空")
# 显式 write_roots 优先于档位默认值（用户显式配置不能被架空，但档位要能覆盖它）
pol = permissions.policy_for("strict", {"write_roots": ["/tmp"]})
if pol["write_roots"] != ["/tmp"]:
    errs.append("显式 write_roots 未生效")
# 显式空黑名单要能真的清空（不是被 or 兜底吃掉）
pol2 = permissions.policy_for("strict", {"denied_commands": []})
if pol2["denied_commands"]:
    errs.append("显式空黑名单未生效")
print("OK" if not errs else "; ".join(errs))
PY
)
if [ "$PERM" = "OK" ]; then
  ok "档位 → 路径根映射正确（strict/standard/open/off）"
else
  bad "档位映射断言失败: $PERM"
fi

# 沙箱子进程：审计里应出现父子两条 agent.run
LOGDIR=~/.local/share/nexus/log/audit
BEFORE=$(cat "$LOGDIR"/$(date +%Y-%m-%d).jsonl 2>/dev/null | grep -c '"agent.run"' || true)
nexus agent run --adapter mock "权限自检" > /dev/null 2>&1
AFTER=$(cat "$LOGDIR"/$(date +%Y-%m-%d).jsonl 2>/dev/null | grep -c '"agent.run"' || true)
if [ "$((AFTER - BEFORE))" -ge 2 ]; then
  ok "agent 确实在沙箱子进程中运行（父子两条 agent.run）"
else
  bad "未检测到沙箱子进程派生 (before=$BEFORE after=$AFTER)"
fi

# 内核级边界：沙箱内写 /etc 必须被只读挂载拦住
SBX=$(systemd-run --user --quiet --wait --pipe --collect \
  -p NoNewPrivileges=yes -p PrivateTmp=yes \
  -p ProtectSystem=strict -p ProtectHome=read-only \
  -- /bin/bash -lc 'echo x > /etc/nexus-selftest-probe 2>/dev/null && echo WROTE || echo BLOCKED' 2>/dev/null | tail -1)
if echo "$SBX" | grep -q "BLOCKED\|Read-only"; then
  ok "内核级边界生效（沙箱内 /etc 不可写）"; else bad "沙箱边界未生效: $SBX"; fi

# ---------------------------------------------------------------- 新手引导
sec "新手引导"
if nexus tutorial --quiet > /dev/null 2>&1; then
  ok "nexus tutorial 可运行"; else bad "tutorial 运行失败"; fi
if [ -f ~/.local/share/nexus/state/tutorial.json ]; then
  ok "引导进度已记录"; else bad "引导进度未落盘"; fi

# ---------------------------------------------------------------- Layer 3
sec "Layer 3 · 事件总线"
if nexus events list 2>&1 | grep -q "crash"; then ok "事件规则表可读"; else bad "事件规则异常"; fi
OUT=$(nexus events test unit_failed --adapter mock 2>&1)
if echo "$OUT" | grep -q "incident_id"; then ok "事件 → 事实快照 → agent → 归档 全链路"; else bad "事件诊断链路异常"; fi
if ls ~/.local/share/nexus/incidents/*/ 2>/dev/null | grep -q "\.md"; then
  ok "诊断归档已落盘"; else bad "诊断归档缺失"; fi

# ---------------------------------------------------------------- 审计
sec "审计"
if nexus audit 2>&1 | grep -q "mcp.call"; then ok "审计记录工具调用"; else bad "审计缺失"; fi

printf '\n\033[1m结果: %d 通过 / %d 失败\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
