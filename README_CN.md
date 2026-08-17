# MiMo Reasoning Content Proxy

[English](README.md) | 简体中文

解决小米 MiMo API 强制要求回传 `reasoning_content` 字段导致 Trae、Cursor 等客户端出现 **400 Param Incorrect** 报错的轻量级代理中间件。

当前版本 **v2.1**。v2.0 起新增 **macOS 客户端**（菜单栏 App + 独立配置窗口），支持配置多个 baseURL 走代理，按路径前缀路由到不同上游。

## 问题背景

2026年5月12日，小米 MiMo API 开放平台发布协议变更：在 Agent 类产品的多轮会话中，如果开启思考模式（Thinking Mode）且历史消息包含工具调用（tool_calls），assistant 消息必须完整回传 `reasoning_content` 字段，否则 API 返回 400 错误。

```
HTTP/1.1 400 Bad Request
{
  "error": {
    "message": "Param Incorrect",
    "param": "The reasoning_content in the thinking mode must be passed back to the API.",
    "code": "400"
  }
}
```

受影响的客户端：Trae、Cursor、GitHub Copilot CLI、Roo Code、Codex、Zed、AutoGen 等。

## 解决方案

本代理作为 Trae 与 MiMo API 之间的中间层：

```
Trae → MiMo Reasoning Proxy → MiMo API
         ↓ 拦截响应，缓存 reasoning_content
         ↓ 下次请求自动注入回 assistant 消息
```

核心逻辑：
1. **拦截响应**：从 MiMo 返回的 assistant 消息中提取 `reasoning_content`，按 `content + tool_calls` 哈希缓存
2. **注入请求**：当 Trae 发送后续请求时，为缺少 `reasoning_content` 的 assistant 消息自动注入缓存值
3. **降级处理**：如果缓存未命中（如代理启动前的旧对话），自动剥离 `tool_calls` 避免 400

## 项目结构

```
mimo-proxy/
├── client/                 # Python 代理核心（Tauri sidecar）
│   ├── __init__.py
│   ├── __main__.py         # 入口：python -m client --cli
│   ├── config.py           # 配置读写
│   └── proxy_core.py      # 代理路由逻辑
├── src/                    # 前端（配置窗口 UI）
│   ├── index.html
│   ├── style.css
│   └── app.js
├── src-tauri/              # Rust 后端（托盘菜单、进程管理）
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── build.rs
│   └── src/
│       ├── lib.rs
│       └── main.rs
├── package.json            # npm 配置
├── requirements.txt        # Python 依赖
├── README.md / README_CN.md
└── LICENSE
```

## 两种使用方式

| 模式 | 命令 | 适用场景 |
|------|------|----------|
| **GUI 客户端**（推荐） | `npm run dev` | macOS 桌面：菜单栏图标 + 配置窗口，可视化增删改 baseURL，启停代理 |
| **CLI 代理** | `python -m client --cli` | 前台运行 uvicorn，读取已保存配置直接启动 |

### 环境要求

- macOS 13+
- Node.js 18+（用于 Tauri 开发）
- Rust（用于编译 Tauri 后端）
- Python 3.10+（推荐 Python 3.12）

```bash
brew update
brew install python@3.12
python3 --version
```

### 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

> 之后每次打开新的终端，都需要先 `source .venv/bin/activate`。

## macOS 客户端使用

### 启动

```bash
npm run dev            # 开发模式（Tauri dev server + 热更新，sidecar 走 .venv 源码）
npm run build:sidecar  # 单独构建 Python sidecar（PyInstaller 独立可执行文件）
npm run build          # 生产构建（先构建 sidecar，再生成 .app / .dmg）
```

打包产物自带 Python 运行时（`src-tauri/resources/sidecar/`），安装后不依赖系统 Python。

启动后菜单栏会出现 ● 图标，自动按上次配置启动代理（可在配置窗口关闭 auto_start）。

### 菜单栏功能

- **状态**: 运行中 / 已停止（只读）
- **启动代理 / 停止代理**: 一键启停
- **打开配置窗口…**: 弹出独立窗口管理端口与 endpoints
- **退出**: 同时停止代理与菜单栏 App

### 配置窗口

| 区域 | 操作 |
|------|------|
| 监听端口 | 直接编辑数字，或点 +/− 步进（保存时校验 1–65535） |
| Endpoints 表格 | 双击单元格编辑 名称 / BaseURL；勾选「默认」切换默认 endpoint；勾选「启用」开关；点击「⎘」复制该行的代理地址 `http://127.0.0.1:{port}/{name}/v1`；点击「−」删除行 |
| 添加 baseURL | 表格下方按钮，自动取一个不冲突的名字（endpoint / endpoint1 / …） |
| ↻ 重载配置 | 从磁盘重新读取（手动改 JSON 后用） |
| 💾 保存并应用 | 写盘 + 若代理在运行则自动重启以应用新配置 |
| 启动代理 / 停止代理 | 与菜单栏按钮等价 |

### 配置存储

配置文件路径：

```
~/.mimo-proxy/config.json
```

可通过环境变量 `MIMO_PROXY_CONFIG_DIR` 覆盖（用于测试或自定义部署）。

配置结构示例：

```json
{
  "host": "127.0.0.1",
  "port": 8899,
  "auto_start": true,
  "cache_max_size": 2000,
  "cache_ttl": 7200,
  "default_name": "default",
  "endpoints": [
    {"name": "default", "base_url": "https://one-api-test.liangyihui.net:8080/v1", "enabled": true},
    {"name": "prod",    "base_url": "https://api.xiaomimimo.com/v1",                "enabled": true}
  ]
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `host` | `127.0.0.1` | 监听地址（仅本机用 127.0.0.1；要局域网访问可改 0.0.0.0） |
| `port` | `8899` | 监听端口 |
| `auto_start` | `true` | 客户端启动时是否自动启动代理 |
| `cache_max_size` | `2000` | 最大缓存条目数 |
| `cache_ttl` | `7200` | 缓存过期时间（秒） |
| `default_name` | `default` | 默认 endpoint 的名称 |
| `endpoints[]` | — | baseURL 列表，每个 endpoint 含 `name` / `base_url` / `enabled` |

### 路径路由

所有 endpoints 共享同一端口，通过 URL 路径前缀区分：

| 请求路径 | 路由到 |
|----------|--------|
| `/{name}/v1/chat/completions` | 该 name 对应的 baseURL |
| `/{name}/chat/completions` | 该 name 对应的 baseURL（兼容，无 `/v1` 前缀） |
| `/{name}/v1/models` | 该 name 对应的 `/models` |
| `/{name}/models` | 该 name 对应的 `/models`（兼容） |
| `/v1/chat/completions` | **默认** endpoint（向后兼容） |
| `/chat/completions` | **默认** endpoint（兼容） |
| `/v1/models` | **默认** endpoint 的 `/models` |
| `/models` | **默认** endpoint 的 `/models`（兼容） |
| `/` | 状态页，列出所有 endpoints 与缓存统计 |
| `/health` | 健康检查 |

示例：

```bash
# 走默认 endpoint（name=default）
curl http://127.0.0.1:8899/v1/chat/completions \
  -H "Authorization: Bearer $MIMO_KEY" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'

# 走指定 endpoint（name=prod）
curl http://127.0.0.1:8899/prod/v1/chat/completions \
  -H "Authorization: Bearer $MIMO_KEY" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

### 配置 Trae

1. 打开 Trae → 设置 → Models → 你的 MiMo 自定义模型
2. 将 **Custom Request URL** 改为以下任一格式：

```
http://127.0.0.1:8899/v1/chat/completions              # 默认 endpoint
http://127.0.0.1:8899/prod/v1/chat/completions         # 指定 endpoint
```

> ⚠️ **常见错误：**
> - ❌ `http://0.0.0.0:8899/v1` — `0.0.0.0` 是监听地址，不能用来访问
> - ❌ `http://127.0.0.1:8899/v1` — 路径不完整
> - ✅ `http://127.0.0.1:8899/v1/chat/completions` — 正确格式
> - ✅ `http://127.0.0.1:8899/{name}/v1/chat/completions` — 多 baseURL 路由

3. API Key 填你的 MiMo API Key
4. Thinking Mode 可以保持开启

## 错误信息

网络类失败会按类型分类，以**中文 message** 返回给客户端（HTTP 502）：

| 底层错误 | 返回给客户端的信息 |
|---|---|
| 连接超时 | 连接上游服务器超时（超过30s）：服务器无响应、网络不通或端口不可达 |
| 读取超时 | 等待上游响应超时（超过300s）：上游处理过慢或已无响应 |
| 连接池排队超时 | 上游连接池已满，排队等待连接超时（超过300s）：并发请求过多 |
| 上游主动断开连接 | 上游在返回响应前断开了连接：网关重启、过载或中间链路被中断 |
| DNS 解析失败 | 无法解析上游域名（DNS 解析失败） |
| 连接被拒绝 | 上游连接被拒绝：服务未启动或端口未监听 |
| TLS/SSL 握手失败 | 与上游 TLS/SSL 握手失败或证书校验不通过 |
| 流中途断开 | 上游流中途中断：上游进程崩溃或网络断开 |

请求最多重试 3 次（指数退避 ~1s / ~2s）后才失败；若已开始向客户端转发数据则不再重试（避免内容重复），以合法的错误帧干净收尾。

## CLI 模式（无 GUI）

```bash
python -m client --cli
```

直接读取 `~/.mimo-proxy/config.json` 在前台运行 uvicorn，不启动菜单栏。适合服务器/SSH 场景。

## Systemd 服务部署（Linux 服务器）

```bash
sudo tee /etc/systemd/system/mimo-proxy.service << 'EOF'
[Unit]
Description=MiMo Reasoning Content Proxy
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/mimo-proxy
ExecStart=/usr/bin/python3 -m client --cli
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
Environment=MIMO_PROXY_CONFIG_DIR=/etc/mimo-proxy

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now mimo-proxy
journalctl -u mimo-proxy -f
```

## 工作原理

```
┌─────────┐     POST /v1/chat/completions     ┌──────────┐
│  Trae   │ ──────────────────────────────────→│  Proxy   │
│         │                                     │          │
│         │  1. 检查 assistant 消息              │          │
│         │     有 tool_calls 但无               │          │
│         │     reasoning_content?              │          │
│         │                                     │          │
│         │  2a. 有缓存 → 注入                   │          │
│         │  2b. 无缓存 → 剥离 tool_calls        │          │
│         │                                     │          │
│         │     ─────────────────────────────→  │  MiMo    │
│         │                                     │  API     │
│         │  3. 缓存响应中的 reasoning_content   │          │
│         │ ←─────────────────────────────────  │          │
│         │                                     │          │
└─────────┘                                     └──────────┘
```

## 已知限制

- 缓存基于内存，重启后丢失（新对话会自动重建）
- 降级处理（剥离 tool_calls）会导致模型丢失工具调用的上下文
- 仅支持 OpenAI 兼容的 `/v1/chat/completions` 端点
- GUI 客户端仅支持 macOS（基于 Tauri 2）


## License

MIT
