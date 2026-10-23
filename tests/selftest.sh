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
# 27B 模型必须被判为不可驻留（16GB 卡）
FIT=$(nexus models list 2>/dev/null | grep -E "27b" | awk '{print $(NF-1)}' | head -1)
if [ "$FIT" = "cpu" ]; then ok "27B 模型被判为 cpu（拒绝驻留）"; else bad "27B 适配判定错误: '$FIT'"; fi
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
