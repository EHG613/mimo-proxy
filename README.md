# MiMo Reasoning Content Proxy

[English](README.md) | [简体中文](README_CN.md)

A lightweight proxy that solves the MiMo API's `reasoning_content` requirement that causes **400 Param Incorrect** errors in Trae, Cursor, and other clients.

Current version: **v2.1**. v2.0 introduced a **macOS desktop client** (menu bar app + config window) built with Tauri 2, supporting multiple baseURLs with path-based routing.

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
    {"name": "default", "base_url": "https://one-api-test.liangyihui.net:8080/v1", "enabled": true, "vendor": "lyh"},
    {"name": "prod",    "base_url": "https://api.xiaomimimo.com/v1",                "enabled": true, "vendor": ""},
    {"name": "agent",   "base_url": "", "enabled": true, "type": "agent", "provider": "codebuddy"}
  ]
}
```

### Vendor Prefix for Model IDs

Some AI tools (e.g. Trae, Cursor) let a custom model override a built-in one when both share the same model ID (such as `deepseek-v4-flash`). To avoid the conflict, set a `vendor` on the endpoint and configure the model ID as `vendor/model-id`:

| What | How |
|------|-----|
| Endpoint config | Fill in the **vendor** column, e.g. `lyh` |
| Model ID in the tool | Configure as `lyh/deepseek-v4-flash` |
| Proxy behavior | When a request routes to that endpoint and `model` starts with `lyh/`, the prefix is stripped and the real model ID `deepseek-v4-flash` is sent upstream |

Notes:

- An empty `vendor` (default) means no stripping — model IDs are forwarded unchanged, identical to previous behavior
- `vendor` must not contain `/` (it is the separator in `vendor/model-id`)
- Prefix stripping only applies to the vendor configured on the endpoint the request is routed to

### Built-in Agent Endpoint (codebuddy-agent-sdk)

In addition to forwarding HTTP upstreams, the proxy ships a built-in "Agent endpoint": set an endpoint's `type` to `agent` and it routes to the local CodeBuddy Agent SDK (Python package `codebuddy-agent-sdk`, the same source as npm `@tencent-ai/agent-sdk`) instead of an HTTP upstream, converting its output into OpenAI-compatible streaming responses.

| What | How |
|------|-----|
| Endpoint config | Set `type` to `agent`, leave `base_url` empty, `provider` defaults to `codebuddy` |
| Request URL | Same as other endpoints: `http://127.0.0.1:{port}/{name}/v1/chat/completions` |
| Request format | Standard OpenAI `/chat/completions`, streaming (`stream: true`) returns SSE |

```bash
curl -N http://127.0.0.1:8899/agent/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v3.1","stream":true,"messages":[{"role":"user","content":"explain recursion"}]}'
```

Notes:

- **Multi-turn context**: sessions are reused per `endpoint name + model` by default; pass an `X-Session-Id` header to pin a specific session (sessions are isolated from each other).
- **Message mapping**: the last `user` message is sent as this turn's prompt; `system` messages are prepended on the first turn.
- **Streaming**: Agent text increments are converted into OpenAI `choices[].delta.content` SSE frames, ending with `data: [DONE]`.
- **Errors**: missing SDK / CLI not found / connection failure return HTTP 502 with a readable `message`.
- **Dependencies**: requires `codebuddy-agent-sdk` (already in `requirements.txt`); in production bundles the CodeBuddy CLI must be available, set `CODEBUDDY_CLI_PATH` if needed.
- **Extensibility**: additional vendors are added via the provider abstraction in `client/agent_providers.py` without touching routing.

### Path Routing

All endpoints share the same port, differentiated by URL path prefix:

| Request Path | Routes To |
|-------------|-----------|
| `/{name}/v1/chat/completions` | Named endpoint's baseURL |
| `/{name}/chat/completions` | Named endpoint's baseURL (no `/v1` prefix, compatibility) |
| `/{name}/v1/models` | Named endpoint's `/models` |
| `/{name}/models` | Named endpoint's `/models` (compatibility) |
| `/v1/chat/completions` | **Default** endpoint (backward compatible) |
| `/chat/completions` | **Default** endpoint (compatibility) |
| `/v1/models` | **Default** endpoint's `/models` |
| `/models` | **Default** endpoint's `/models` (compatibility) |
| `/` | Status page, lists all endpoints |
| `/health` | Health check |

### Configure Trae

1. Trae → Settings → Models → Your MiMo custom model
2. Set **Custom Request URL** to:

```
http://127.0.0.1:8899/v1/chat/completions              # Default endpoint
http://127.0.0.1:8899/{name}/v1/chat/completions       # Named endpoint
```

## Error Messages

Network failures are classified and returned to the client with human-readable Chinese `message` text (HTTP 502):

| Underlying error | Message shown to the client |
|---|---|
| Connect timeout | 连接上游服务器超时（超过30s）：服务器无响应、网络不通或端口不可达 |
| Read timeout | 等待上游响应超时（超过300s）：上游处理过慢或已无响应 |
| Pool timeout | 上游连接池已满，排队等待连接超时（超过300s）：并发请求过多 |
| Upstream closed connection | 上游在返回响应前断开了连接：网关重启、过载或中间链路被中断 |
| DNS resolution failure | 无法解析上游域名（DNS 解析失败） |
| Connection refused | 上游连接被拒绝：服务未启动或端口未监听 |
| TLS/SSL handshake failure | 与上游 TLS/SSL 握手失败或证书校验不通过 |
| Mid-stream disconnect | 上游流中途中断：上游进程崩溃或网络断开 |

Requests are retried up to 3 times with exponential backoff (~1s / ~2s) before failing. Once a stream has started sending data to the client it is finished with a clean error frame instead of being retried, to avoid duplicated content.

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

## License

MIT
