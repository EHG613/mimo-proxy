# MiMo Reasoning Content Proxy

[English](README.md) | [简体中文](README_CN.md)

A lightweight proxy that solves the MiMo API's `reasoning_content` requirement that causes **400 Param Incorrect** errors in Trae, Cursor, and other clients.

v2.0 introduces a **macOS desktop client** (menu bar app + config window) built with Tauri 2, supporting multiple baseURLs with path-based routing.

## Project Structure

```
mimo-proxy/
├── client/                 # Python proxy core (Tauri sidecar)
│   ├── __init__.py
│   ├── __main__.py         # Entry: python -m client --cli
│   ├── config.py           # Config read/write
│   └── proxy_core.py      # Proxy routing logic
├── src/                    # Frontend (config window UI)
│   ├── index.html
│   ├── style.css
│   └── app.js
├── src-tauri/              # Rust backend (tray menu, process management)
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── build.rs
│   └── src/
│       ├── lib.rs
│       └── main.rs
├── package.json            # npm config
├── requirements.txt        # Python dependencies
├── README.md / README_CN.md
└── LICENSE
```

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

受影响的客户端包括：Trae、Cursor、GitHub Copilot CLI、Roo Code、Codex、Zed、AutoGen 等。

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

## Quick Start

### Requirements

- macOS 13+
- Node.js 18+ (for Tauri)
- Rust (for Tauri backend)
- Python 3.10+ (Python 3.12 recommended)

```bash
# Install Python 3.12
brew update
brew install python@3.12

# Verify
python3 --version
```

### Setup

```bash
# Create virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# Install npm dependencies
npm install
```

### Run

```bash
npm run dev            # Dev mode (Tauri dev server + hot reload, sidecar runs from .venv)
npm run build:sidecar  # Build the Python sidecar only (PyInstaller standalone binary)
npm run build          # Production build (sidecar first, then .app / .dmg)
```

The bundle ships its own Python runtime (`src-tauri/resources/sidecar/`) — no system Python required after install.

## Usage

| Mode | Command | Use Case |
|------|---------|----------|
| **GUI Client** (recommended) | `npm run dev` | macOS desktop: menu bar icon + config window |
| **CLI** | `python -m client --cli` | Foreground uvicorn, reads saved config |

### Config Storage

```
~/.mimo-proxy/config.json
```

Example:

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

### Path Routing

All endpoints share the same port, differentiated by URL path prefix:

| Request Path | Routes To |
|-------------|-----------|
| `/{name}/v1/chat/completions` | Named endpoint's baseURL |
| `/v1/chat/completions` | **Default** endpoint (backward compatible) |
| `/` | Status page, lists all endpoints |
| `/health` | Health check |

### Configure Trae

1. Trae → Settings → Models → Your MiMo custom model
2. Set **Custom Request URL** to:

```
http://127.0.0.1:8899/v1/chat/completions              # Default endpoint
http://127.0.0.1:8899/{name}/v1/chat/completions       # Named endpoint
```

## Systemd Deployment (Linux Server)

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

## Limitations

- Cache is in-memory, lost on restart (new conversations rebuild automatically)
- Degraded mode (stripping tool_calls) causes the model to lose tool call context
- Only supports OpenAI-compatible `/v1/chat/completions` endpoint
- GUI client is macOS only (built on Tauri 2)

## 邀请码
我在用 MiMo 开放平台体验 小米顶尖模型 MiMo V2.5等 ，通过我的邀请码注册为新用户，即得 ¥10 API 体验金。邀请码：B8DMC5。注册：https://platform.xiaomimimo.com?ref=B8DMC5（注册后点控制台左下方入口填入，体验金40天有效）


## 相关链接

- [小米 MiMo API 官方公告](https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/passing-back-reasoning_content)
- [LINUX DO 讨论帖](https://linux.do/t/topic/2165444)
- [Trae 论坛反馈](https://forum.trae.cn/t/topic/17335)

## License

MIT
