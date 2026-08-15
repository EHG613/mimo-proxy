#!/usr/bin/env bash
# 独立生成 DMG（绕过 tauri bundle_dmg.sh 的 AppleScript 布局步骤）。
# 适用于任何环境（SSH / 受限终端 / 无 Finder 自动化权限），
# 产物为标准 UDZO 压缩镜像：挂载后可见 App 图标与 Applications 替身，拖拽即完成安装。
set -euo pipefail
cd "$(dirname "$0")/.."

APP="src-tauri/target/release/bundle/macos/MiMo Proxy.app"
[ -d "$APP" ] || { echo "未找到 $APP（先运行 npm run build）"; exit 1; }

VERSION=$(node -p "require('./package.json').version")
OUT="src-tauri/target/release/bundle/dmg/MiMo Proxy_${VERSION}_aarch64.dmg"
mkdir -p "src-tauri/target/release/bundle/dmg"

STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

rm -f "$OUT"
# -srcfolder 模式直接打包，无需挂载 /Volumes
hdiutil create -volname "MiMo Proxy" -srcfolder "$STAGING" -format UDZO -ov "$OUT" >/dev/null
hdiutil verify "$OUT" >/dev/null && echo "DMG 生成完成: $OUT"
