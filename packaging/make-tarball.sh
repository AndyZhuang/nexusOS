#!/usr/bin/env bash
# 打包 NexusOS 产物。
#
# 产物结构（解包后即为 payload/）：
#   lib/nexus/...        Python 包
#   bin/nexus            控制面入口
#   bin/nexus-mcp        MCP server
#   share/systemd/*      systemd --user 单元
#   migrations/*.sh      配置迁移
#   VERSION              版本号
#
# 产物内容不含任何机器专属路径（入口脚本在安装时按实际路径生成）。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
OUT="${1:-$REPO/release}"

VERSION="$(tr -d '[:space:]' < "$REPO/VERSION")"
NAME="nexusos-$VERSION"
TARBALL="$OUT/$NAME.tar.gz"

mkdir -p "$OUT"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PAYLOAD="$STAGE/payload"
mkdir -p "$PAYLOAD/lib" "$PAYLOAD/bin" "$PAYLOAD/share/systemd" "$PAYLOAD/migrations"

# 1) Python 包
cp -r "$REPO/src/nexus" "$PAYLOAD/lib/nexus"
find "$PAYLOAD/lib" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PAYLOAD/lib" -name '*.pyc' -delete 2>/dev/null || true

# 2) 入口脚本（安装时会被 nexus-bootstrap 重新生成，这里放一份通用版）
cat > "$PAYLOAD/bin/nexus" <<'EOF'
#!/bin/sh
# NexusOS 控制面入口（通用版；正式安装由 nexus-bootstrap 写入绝对路径）
NEXUS_LIB="${NEXUS_LIB:-$HOME/.local/share/nexus/base/lib}"
export PYTHONPATH="$NEXUS_LIB${PYTHONPATH:+:$PYTHONPATH}"
export NEXUS_DATA_HOME="${NEXUS_DATA_HOME:-$HOME/.local/share/nexus}"
export NEXUS_CONFIG_HOME="${NEXUS_CONFIG_HOME:-$HOME/.config/nexus}"
exec python3 -m nexus.cli "$@"
EOF
cat > "$PAYLOAD/bin/nexus-mcp" <<'EOF'
#!/bin/sh
NEXUS_LIB="${NEXUS_LIB:-$HOME/.local/share/nexus/base/lib}"
export PYTHONPATH="$NEXUS_LIB${PYTHONPATH:+:$PYTHONPATH}"
export NEXUS_DATA_HOME="${NEXUS_DATA_HOME:-$HOME/.local/share/nexus}"
export NEXUS_CONFIG_HOME="${NEXUS_CONFIG_HOME:-$HOME/.config/nexus}"
exec python3 -m nexus.mcp "$@"
EOF
chmod +x "$PAYLOAD/bin/nexus" "$PAYLOAD/bin/nexus-mcp"

# 3) systemd 单元
cp "$REPO"/systemd/nexus-* "$PAYLOAD/share/systemd/" 2>/dev/null || true

# 4) 迁移脚本
cp "$REPO"/migrations/*.sh "$PAYLOAD/migrations/" 2>/dev/null || true

# 5) 版本与元信息
echo "$VERSION" > "$PAYLOAD/VERSION"
COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
{
  echo "name: nexusos"
  echo "version: $VERSION"
  echo "commit: $COMMIT"
  echo "channel: ${NEXUS_CHANNEL:-stable}"
  echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "python: $(python3 -V 2>&1)"
} > "$PAYLOAD/BUILDINFO"

# 6) 打包 + 校验和
tar -czf "$TARBALL" -C "$STAGE" payload
cd "$OUT"
sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256"

echo "产物: $TARBALL"
echo "大小: $(du -h "$TARBALL" | cut -f1)"
echo "校验: $(cat "$NAME.tar.gz.sha256")"
