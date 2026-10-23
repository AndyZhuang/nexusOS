#!/usr/bin/env bash
# NexusOS 迁移脚本 —— 时间戳命名，幂等，每用户只跑一次。
#
# 约定（学习 Omarchy 的 migrations/ 机制）：
#   * 文件名 = 首次发布该迁移的 Unix 时间戳 + .sh
#   * 脚本必须幂等：重复执行不产生副作用
#   * 失败必须返回非 0 —— 更新流程会因此中止
#   * 只改用户配置，不改系统目录（rootless）
set -euo pipefail

echo "迁移: 初始配置布局（建立审计目录与默认提示词）"

DATA="${NEXUS_DATA_HOME:-$HOME/.local/share/nexus}"
CFG="${NEXUS_CONFIG_HOME:-$HOME/.config/nexus}"

mkdir -p "$DATA/log/audit" "$DATA/log/events" "$DATA/log/gateway" \
         "$DATA/agent/sessions" "$DATA/agent/memory" "$DATA/incidents" \
         "$DATA/state/migrations"
mkdir -p "$CFG/prompts"

if [ ! -f "$CFG/prompts/system.md" ]; then
  cat > "$CFG/prompts/system.md" <<'EOF'
你是 NexusOS 的系统代理，运行在一台由 NexusOS 管理的 Linux 工作站上。

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
EOF
  echo "  已写入默认 system prompt"
else
  echo "  system prompt 已存在，跳过"
fi

echo "迁移完成"
