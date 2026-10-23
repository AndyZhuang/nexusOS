#!/usr/bin/env bash
# 权限体系升级：把写死的路径根改成"由档位推导"。
#
# 背景：v0.2 的 policy.toml 里直接写死了 read_roots / write_roots，
# 而策略层的可写根与沙箱档位完全脱钩 —— 结果是：
#   ① 真正生效的只有策略层一层，local 适配器的工具调用没进 systemd 沙箱；
#   ② 用户切不了档位（显式 write_roots 会架空档位默认值）。
#
# 本迁移：移除写死的 read_roots / write_roots，写入 default_tier = "standard"。
# 迁移后由 nexus permissions 统一管理档位，路径根随档位推导。
set -euo pipefail

echo "迁移: 权限策略改为档位驱动（default_tier = standard）"

CFG="${NEXUS_CONFIG_HOME:-$HOME/.config/nexus}"
POLICY="$CFG/policy.toml"

if [ ! -f "$POLICY" ]; then
  echo "  policy.toml 不存在（全新安装无需迁移），跳过"
  exit 0
fi

python3 - "$POLICY" <<'PY'
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    text = fh.read()

removed = []
kept = []
for line in text.splitlines():
    stripped = line.strip()
    if stripped.startswith("read_roots") or stripped.startswith("write_roots"):
        removed.append(stripped.split("=", 1)[0].strip())
        continue
    kept.append(line)

if any(l.strip().startswith("default_tier") for l in kept):
    kept = [re.sub(r'default_tier\s*=\s*"[^"]*"', 'default_tier = "standard"', l)
            if l.strip().startswith("default_tier") else l
            for l in kept]
else:
    kept.insert(0, 'default_tier = "standard"')

with open(path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(kept).rstrip() + "\n")

print("  已移除写死的路径配置: " + (", ".join(removed) if removed else "(无)"))
print("  已设置 default_tier = standard")
print("  提示: 之后用 `nexus permissions set <档位>` 调整权限")
PY

echo "迁移完成"
