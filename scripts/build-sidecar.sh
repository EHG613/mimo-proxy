#!/usr/bin/env bash
# 构建 Python sidecar 独立可执行文件（PyInstaller onedir）→ src-tauri/resources/sidecar/
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x .venv/bin/python ] || { echo "缺少 .venv：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
.venv/bin/pip show pyinstaller >/dev/null 2>&1 || .venv/bin/pip install "pyinstaller>=6"

rm -rf src-tauri/resources/sidecar build/pyinstaller
# PyInstaller 6 的缓存目录默认在 ~/Library/Application Support/pyinstaller，
# 本机可能因权限（Operation not permitted）无法创建，重定向到项目内
export PYINSTALLER_CONFIG_DIR="$PWD/build/pyinstaller-cache"
.venv/bin/pyinstaller \
  --noconfirm --clean \
  --name mimo-proxy-sidecar \
  --distpath src-tauri/resources/sidecar \
  --workpath build/pyinstaller --specpath build/pyinstaller \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on \
  sidecar_entry.py
echo "sidecar → src-tauri/resources/sidecar/mimo-proxy-sidecar/"
