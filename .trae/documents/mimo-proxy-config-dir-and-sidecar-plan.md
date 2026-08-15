# mimo-proxy：配置目录迁移 ~/.mimo-proxy + PyInstaller sidecar 打包

用户确认两项都要，按序执行：**任务 1 先做（目录迁移），任务 2 后做（sidecar 独立打包）**。

---

## 概述

1. **任务 1**：配置/日志目录从 `~/Library/Application Support/MiMoProxy/` 迁移到 `~/.mimo-proxy/`，带一次性自动迁移，旧目录保留作为天然备份。
2. **任务 2**：用 PyInstaller 把 `client/` 打成独立可执行 sidecar，打进 DMG 资源，安装后不再依赖用户机器上的 Python。开发模式继续用 `.venv` 直跑源码。

## 现状分析（Phase 1 探索结论）

- 路径引用点共 5 处代码 + 2 个 README：
  - [src-tauri/src/lib.rs#L63-L68](file:///Users/admin/Projects/mimo-proxy/src-tauri/src/lib.rs) `config_path()`：拼 `HOME/Library/Application Support/MiMoProxy/config.json`，并通过 `MIMO_PROXY_CONFIG_DIR` 环境变量传给 sidecar
  - [client/config.py#L38-L50](file:///Users/admin/Projects/mimo-proxy/client/config.py) `_app_support_dir()`：环境变量优先，默认同旧目录
  - [client/config.py#L1](file:///Users/admin/Projects/mimo-proxy/client/config.py) docstring、[client/__main__.py#L125](file:///Users/admin/Projects/mimo-proxy/client/__main__.py) 错误提示文本：硬编码旧路径文案
  - README.md / README_CN.md：各 2-3 处旧路径
- sidecar 启动逻辑（lib.rs `start_proxy`）：生产包预期 `resource_dir()/client`（python -m client），回退顺序 `repo/.venv/bin/python` → 系统 `python3`；tauri.conf.json `bundle.resources: ["../client"]` 只打包了 Python **源码**，没打包解释器 → 终端用户机器无 python3 时启动失败
- 依赖：`requirements.txt` 仅 httpx/starlette/uvicorn；`client/__main__.py` 用**相对导入**（`from .config import ...`），PyInstaller 直接以它为入口会因缺包上下文失败，需要绝对导入的入口 wrapper
- 已有的进程生命周期机制（stdin 管道 EOF + ppid watchdog、`--cli` 参数、`MIMO_PROXY_CONFIG_DIR` 环境变量）与启动方式解耦，换成 PyInstaller 二进制后**无需改动**

## 任务 1：配置目录迁移到 ~/.mimo-proxy/

### 1.1 [src-tauri/src/lib.rs](file:///Users/admin/Projects/mimo-proxy/src-tauri/src/lib.rs) — `config_path()` 重写

```rust
fn config_path() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    let dir = std::path::PathBuf::from(home).join(".mimo-proxy");
    std::fs::create_dir_all(&dir).ok();
    let cfg = dir.join("config.json");
    // 一次性迁移：旧版目录为 ~/Library/Application Support/MiMoProxy（保留不删，作为回滚备份）
    if !cfg.exists() {
        let legacy = std::path::PathBuf::from(&home)
            .join("Library/Application Support/MiMoProxy/config.json");
        if legacy.exists() {
            let _ = std::fs::copy(&legacy, &cfg);
        }
    }
    cfg
}
```

### 1.2 [client/config.py](file:///Users/admin/Projects/mimo-proxy/client/config.py) — 默认目录同步

- `_app_support_dir()` 更名 `_config_dir()`（旧名不再贴切，仅一处调用），默认分支改为 `Path(home) / ".mimo-proxy"`，环境变量优先逻辑不变
- 更新文件头部 docstring 的路径说明
- 旧日志（`Application Support/MiMoProxy/logs/`）**不迁移**，自然留在原地

### 1.3 [client/__main__.py#L125](file:///Users/admin/Projects/mimo-proxy/client/__main__.py) — 错误提示文本改为 `~/.mimo-proxy/config.json`

### 1.4 README.md / README_CN.md — 同步替换旧路径文案（约 5 处）

### 1.5 验证（任务 1）

1. `cargo check` 通过；touch lib.rs 触发 dev 热重载
2. 确认 `~/.mimo-proxy/config.json` 生成且内容 = 旧配置（含 airouter endpoint）；旧目录原样保留
3. 跑一次代理请求后确认 `~/.mimo-proxy/logs/proxy.log` 有新写入；`get_config`/保存并应用 链路正常

## 任务 2：PyInstaller sidecar 独立打包

### 2.1 新增 `sidecar_entry.py`（仓库根目录）— PyInstaller 入口 wrapper

```python
"""PyInstaller 打包入口：client/__main__.py 使用相对导入，直接作为脚本入口
会因缺包上下文失败，这里以绝对导入建立包上下文。"""
from client.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
```

### 2.2 新增 `scripts/build-sidecar.sh` — 构建脚本（自包含、幂等）

```bash
#!/usr/bin/env bash
# 构建 Python sidecar 独立可执行文件（PyInstaller onedir）→ src-tauri/resources/sidecar/
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x .venv/bin/python ] || { echo "缺少 .venv：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
.venv/bin/pip show pyinstaller >/dev/null 2>&1 || .venv/bin/pip install "pyinstaller>=6"

rm -rf src-tauri/resources/sidecar build/pyinstaller
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
```

说明：uvicorn 运行时动态 `import` 各 backend 模块，PyInstaller 静态分析扫不到，必须 `--hidden-import`，否则打包产物启动即 `ModuleNotFoundError`（已知坑）。

### 2.3 [src-tauri/tauri.conf.json](file:///Users/admin/Projects/mimo-proxy/src-tauri/tauri.conf.json) — 资源清单

`bundle.resources` 由 `["../client"]` 改为 `["resources/sidecar"]`：不再打包 Python 源码（终端用户机器无解释器，源码包无用），只打包 onedir 产物。

### 2.4 [src-tauri/src/lib.rs](file:///Users/admin/Projects/mimo-proxy/src-tauri/src/lib.rs) — `start_proxy` 启动解析重写

解析顺序（两者都设 `MIMO_PROXY_CONFIG_DIR` + `stdin(Stdio::piped())`，监控/重启逻辑不动）：

1. **生产**：`resource_dir()/sidecar/mimo-proxy-sidecar/mimo-proxy-sidecar` 存在 → 直接 `Command::new(bin).args(["--cli"]).current_dir(bin.parent())`
2. **开发**：`repo/.venv/bin/python` 存在 → 维持现状 `python -m client --cli`（cwd/PYTHONPATH 逻辑不变）
3. 都没有 → 报错 `"未找到代理 sidecar：生产包缺少 resources/sidecar（运行 npm run build:sidecar 重新打包），开发环境缺少 .venv/bin/python"`

**删除**系统 `python3` 回退（终端用户机器即使有 python3 也没有 client 源码了，该分支只会产生误导性失败）。删除原 `resource_dir()/client` 过滤分支（资源里已无此目录）。

### 2.5 [package.json](file:///Users/admin/Projects/mimo-proxy/package.json) — 脚本

```json
"build:sidecar": "bash scripts/build-sidecar.sh",
"build": "npm run build:sidecar && tauri build"
```

`build` 链式先建 sidecar，保证 DMG 内一定有 sidecar。

### 2.6 `.gitignore` — 追加 `/build/` 与 `/src-tauri/resources/`（均为构建产物）

### 2.7 README（中英）— 构建小节各加一行 `npm run build:sidecar` 说明

### 2.8 验证（任务 2）

1. `bash scripts/build-sidecar.sh` 成功产出二进制
2. **冒烟测试**（不与运行中代理抢端口）：临时目录写 port 8897 的 config，`MIMO_PROXY_CONFIG_DIR=/tmp/... sidecar --cli` 后 `curl 127.0.0.1:8897/health` 返回正常，再验证 stdin EOF（关掉管道写端进程 ~1s 内 sidecar 退出，机制与解释器无关但需实证）
3. `cargo check` + dev 热重载：开发模式仍走 `.venv`，代理正常启停
4. `npm run build` 全量打包（release 编译需数分钟）：确认 `src-tauri/target/release/bundle/macos/*.app/Contents/Resources/sidecar/mimo-proxy-sidecar/` 存在且二进制可执行；DMG 生成于 `bundle/dmg/`

## 假设与决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 迁移范围 | 仅 `config.json`，旧目录整体保留 | 日志无迁移价值；旧目录即回滚备份 |
| 新旧配置同时存在 | 新目录优先，不回写旧目录 | 简单、可预期 |
| 打包形态 | PyInstaller **onedir**（非 onefile） | 启动快、无解压开销；onedir 整目录入 Resources |
| 删除系统 python3 回退 | 是 | 生产无源码、该分支必然失败，报错信息更明确 |
| dev 是否也用 sidecar 二进制 | 否，dev 固定 `.venv` 源码 | 改 Python 代码无需重打 sidecar，迭代快 |
| 架构 | 仅 arm64（本机构建） | 用户目标机为本人 macOS；universal binary 超出范围 |

## 风险提示

- onedir 体积约 30-60MB，DMG 相应变大——可接受
- 未做正式 codesign/公证：本地分发首次打开可能需右键打开（与现状一致，非本次范围）
- `resource_dir()` 在 dev 下的解析行为不确定是否命中 `src-tauri/resources/sidecar`：若命中则 dev 也用 sidecar（功能等价，无碍）；预期是不命中走 venv

## 执行顺序

任务 1（1.1 → 1.4 → 验证）→ 任务 2（2.1 → 2.6 → 冒烟 → 全量打包验证）→ README 收尾 → 更新项目记忆
