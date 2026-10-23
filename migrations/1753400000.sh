#!/usr/bin/env bash
# 补充"被拒绝的操作必须显式上报"规则。
#
# 背景：实测中 agent 在 /etc 写入被策略拒绝后，改到可写区完成了操作，
# 但最终答复只说"文件已成功创建"，没有告知用户原请求被拒 —— 会误导人。
# 本迁移把该规则追加进已有的 system prompt（幂等：已存在则不重复追加）。
set -euo pipefail

echo "迁移: system prompt 增加「被拒绝操作必须显式上报」规则"

CFG="${NEXUS_CONFIG_HOME:-$HOME/.config/nexus}"
PROMPT="$CFG/prompts/system.md"
MARK="关于被拒绝的操作（重要）"

mkdir -p "$CFG/prompts"

if [ ! -f "$PROMPT" ]; then
  echo "  system prompt 不存在（由初始迁移负责创建），跳过"
  exit 0
fi

if grep -qF "$MARK" "$PROMPT"; then
  echo "  规则已存在，跳过"
  exit 0
fi

cat >> "$PROMPT" <<'EOF'

关于被拒绝的操作（重要）：
如果某个工具调用被拒绝（策略限制、只读挂载、沙箱拦截），你必须在最终答复里
明确写出来：原请求是什么、被拒绝的原因、你随后改做了什么（或建议用户怎么做）。
绝对不要让用户以为原操作已经完成 —— 尤其不要用"已成功创建"这类措辞
去描述一个实际发生在别的路径上的替代操作。
EOF

echo "  已追加规则"
