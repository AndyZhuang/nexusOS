#!/usr/bin/env bash
# 构建 Ubuntu deb 包（系统级 profile）。
#
# 与 tarball 的分工：
#   tarball —— rootless profile，装在用户目录，无需 root
#   deb     —— 系统级 profile，落到 /usr 与 /etc，systemd 系统单元随包启动
#
# dpkg-deb --build 不需要 root。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
OUT="${1:-$REPO/release}"

VERSION="$(tr -d '[:space:]' < "$REPO/VERSION")"
PKG="nexusos_${VERSION}_amd64"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
DEST="$STAGE/$PKG"

mkdir -p "$DEST/DEBIAN" \
         "$DEST/usr/lib/nexusos/nexus" \
         "$DEST/usr/local/bin" \
         "$DEST/etc/systemd/system" \
         "$DEST/etc/nexus" \
         "$DEST/usr/share/doc/nexusos"

# 1) Python 包
cp -r "$REPO/src/nexus/." "$DEST/usr/lib/nexusos/nexus/"
find "$DEST/usr/lib/nexusos" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST/usr/lib/nexusos" -name '*.pyc' -delete 2>/dev/null || true

# 2) 入口脚本（系统路径）
cat > "$DEST/usr/local/bin/nexus" <<'EOF'
#!/bin/sh
export PYTHONPATH="/usr/lib/nexusos${PYTHONPATH:+:$PYTHONPATH}"
export NEXUS_DATA_HOME="${NEXUS_DATA_HOME:-/var/lib/nexus}"
export NEXUS_CONFIG_HOME="${NEXUS_CONFIG_HOME:-/etc/nexus}"
exec python3 -m nexus.cli "$@"
EOF
cat > "$DEST/usr/local/bin/nexus-mcp" <<'EOF'
#!/bin/sh
export PYTHONPATH="/usr/lib/nexusos${PYTHONPATH:+:$PYTHONPATH}"
export NEXUS_DATA_HOME="${NEXUS_DATA_HOME:-/var/lib/nexus}"
export NEXUS_CONFIG_HOME="${NEXUS_CONFIG_HOME:-/etc/nexus}"
exec python3 -m nexus.mcp "$@"
EOF
chmod 0755 "$DEST/usr/local/bin/nexus" "$DEST/usr/local/bin/nexus-mcp"

# 3) systemd 系统单元（路径按系统 profile 填充）
for f in "$REPO"/systemd/nexus-*.service "$REPO"/systemd/nexus-*.timer; do
  [ -f "$f" ] || continue
  base="$(basename "$f")"
  sed -e "s|__NEXUS_DATA__|/var/lib/nexus|g" \
      -e "s|__BASE_LIB__|/usr/lib/nexusos|g" \
      -e "s|__PYTHON__|/usr/bin/python3|g" \
      -e "s|^User=.*||" \
      "$f" > "$DEST/etc/systemd/system/$base"
done

# 4) 系统层默认配置（/etc/nexus 是只读默认层，用户层覆盖）
cat > "$DEST/etc/nexus/models.toml" <<'EOF'
default_model = ""

[backends.ollama]
url = "http://127.0.0.1:11434"
kind = "local"
models_dir = ""
keep_alive = "30m"

[backends.vllm]
url = "http://127.0.0.1:8000"
kind = "remote"
enabled = false

[gpu]
reserve_mb = 1024
model_cap_mb = 12288
EOF

# 5) 迁移脚本
mkdir -p "$DEST/usr/lib/nexusos/migrations"
cp "$REPO"/migrations/*.sh "$DEST/usr/lib/nexusos/migrations/" 2>/dev/null || true

# 6) 文档
cp "$REPO/README.md" "$DEST/usr/share/doc/nexusos/README.md" 2>/dev/null || true
[ -f "$REPO/CHANGELOG.md" ] && cp "$REPO/CHANGELOG.md" "$DEST/usr/share/doc/nexusos/CHANGELOG.md"

# 7) control
INSTALLED_SIZE="$(du -ks "$DEST" | cut -f1)"
cat > "$DEST/DEBIAN/control" <<EOF
Package: nexusos
Version: $VERSION
Section: admin
Priority: optional
Architecture: amd64
Maintainer: NexusOS Maintainers <maintainers@nexusos.dev>
Depends: python3 (>= 3.11), systemd
Recommends: ollama
Description: AI-native system layer for personal AI workstations
 NexusOS is an AI-native operating-system layer: system events drive agents,
 agents can change system state through a natural-language control surface, and
 VRAM arbitration keeps local models inside an explicit budget.
 .
 This package installs the system-level profile (systemd system units under
 /etc/systemd/system). A rootless profile is available as a plain tarball that
 installs into the user directory without root.
EOF

# 8) postinst / prerm
cat > "$DEST/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
case "$1" in
  configure)
    mkdir -p /var/lib/nexus/log/audit /var/lib/nexus/log/events \
             /var/lib/nexus/log/gateway /var/lib/nexus/agent/sessions \
             /var/lib/nexus/agent/memory /var/lib/nexus/incidents \
             /var/lib/nexus/state/migrations
    chown -R root:root /var/lib/nexus 2>/dev/null || true
    chmod 0755 /var/lib/nexus
    if command -v systemctl >/dev/null 2>&1; then
      systemctl daemon-reload || true
      for u in nexus-gateway nexus-broker nexus-event-router; do
        systemctl enable "$u.service" >/dev/null 2>&1 || true
        systemctl restart "$u.service" >/dev/null 2>&1 || true
      done
    fi
    ;;
esac
exit 0
EOF

cat > "$DEST/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
  remove|upgrade|deconfigure)
    if command -v systemctl >/dev/null 2>&1; then
      for u in nexus-gateway nexus-broker nexus-event-router nexus-ollama; do
        systemctl stop "$u.service" >/dev/null 2>&1 || true
        systemctl disable "$u.service" >/dev/null 2>&1 || true
      done
      systemctl daemon-reload || true
    fi
    ;;
esac
exit 0
EOF
chmod 0755 "$DEST/DEBIAN/postinst" "$DEST/DEBIAN/prerm"

# 9) 构建
mkdir -p "$OUT"
if command -v dpkg-deb >/dev/null 2>&1; then
  dpkg-deb --build --root-owner-group "$DEST" "$OUT/$PKG.deb" >/dev/null
  echo "deb: $OUT/$PKG.deb  ($(du -h "$OUT/$PKG.deb" | cut -f1))"
else
  warn_no_dpkg=1
  echo "!! 未找到 dpkg-deb，跳过 deb 构建（仅 tarball 可用）"
fi

# 10) 系统层版本记录
mkdir -p "$DEST/usr/lib/nexusos"
echo "$VERSION" > "$DEST/usr/lib/nexusos/VERSION"

echo "完成。"
