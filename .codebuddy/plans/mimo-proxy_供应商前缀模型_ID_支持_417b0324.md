---
name: mimo-proxy 供应商前缀模型 ID 支持
overview: 为 mimo-proxy 每个 endpoint 增加 vendor（供应商）配置，支持「供应商/模型id」格式的模型 id：代理收到请求后剥离匹配的供应商前缀，以真实模型 id 转发上游，解决 AI 工具自定义模型 id 与内置模型 id 冲突的问题。
todos:
  - id: endpoint-vendor-config
    content: 为 Endpoint 新增 vendor 字段并打通 Python/Rust 配置读写，旧配置自动补空
    status: completed
  - id: model-prefix-strip
    content: 在 proxy_core.py 转发前实现模型 ID 供应商前缀剥离逻辑并记录日志
    status: completed
    dependencies:
      - endpoint-vendor-config
  - id: frontend-vendor-ui
    content: 前端表格新增供应商列、编辑与保存校验，新增行默认空 vendor
    status: completed
    dependencies:
      - endpoint-vendor-config
  - id: docs-vendor
    content: 更新中英文 README 配置字段说明并新增供应商前缀用法章节
    status: completed
    dependencies:
      - model-prefix-strip
      - frontend-vendor-ui
  - id: verify
    content: 用 mock 上游验证前缀剥离与兼容场景，并执行 cargo check 验证编译
    status: completed
    dependencies:
      - model-prefix-strip
      - frontend-vendor-ui
---

## 用户需求

在 mimo-proxy 中为每个 baseURL（endpoint）配置「供应商」（vendor），用于解决 AI 工具中模型 ID 重复导致用户自定义覆盖内置配置的问题。用户配置模型 ID 时使用「供应商/模型id」格式（如 `lyh/deepseek-v4-flash`），代理收到请求后识别出与当前 endpoint 的供应商匹配的前缀，剥离供应商部分，按真实模型 ID（`deepseek-v4-flash`）转发上游。

## 产品概述

在现有 v2.1 多 endpoint 代理架构上，为每个 endpoint 增加 vendor 配置项。请求经路径前缀路由到指定 endpoint 后，根据该 endpoint 的 vendor 对请求体中的 model 字段做前缀剥离，实现模型 ID 冲突消解，且不改变无配置时的既有行为。

## 核心功能

- endpoint 配置新增 vendor 字段（Python/Rust/前端三端一致，默认空字符串）
- 请求转发前检测 model 是否以 `vendor + "/"` 开头，匹配则剥离前缀后转发真实模型 ID
- 不匹配或 vendor 为空时原样转发，保持旧行为完全兼容
- 前端表格新增「供应商」列，支持编辑与校验（不允许包含 `/`，允许为空）
- 旧配置文件无需迁移即可无缝兼容（缺失 vendor 字段时默认空）

## 技术栈

沿用项目现有技术栈，不引入新依赖：

- 代理核心：Python 3.10+ / Starlette / httpx（client/）
- 桌面壳：Rust / Tauri 2（src-tauri/）
- 前端：原生 HTML / CSS / JS（src/）

## 实现方案

在 endpoint 配置层新增 vendor 字段，在代理请求改写层实现模型 ID 前缀剥离：

1. 配置层（三端对齐）：`client/config.py` 的 `Endpoint` dataclass、`src-tauri/src/lib.rs` 的 `Endpoint` 结构体同步新增 `vendor` 字段，默认空字符串；`Config.from_dict` 用 `e.get("vendor", "")` 读取（旧配置缺字段自动补空），Rust 侧加 `#[serde(default)]` 保证反序列化兼容。
2. 请求改写层：`client/proxy_core.py` 的 `chat_completions` 中，在 `inject_reasoning` 之后、发起上游请求之前处理 model 字段：

- `endpoint.vendor` 非空且 `body["model"]` 以 `vendor + "/"` 开头 → 剥离前缀，`log.info` 记录
- 其余情况原样转发
- 流式与非流式共用同一个 body 对象，修改一处即可覆盖两条转发路径

3. 前端层：表格新增「供应商」列（vendor 输入框），保存时校验不得包含 `/`（与分隔符冲突）、允许为空；新增 endpoint 时默认 vendor 为空。
4. 文档层：README 配置示例补充 vendor 字段，新增「模型 ID 供应商前缀」用法章节。

## 关键决策与边界

- **按路由到的 endpoint 匹配 vendor**：请求经 `/{name}/v1/...` 路径已确定目标 endpoint，直接使用该 endpoint 的 vendor 做前缀匹配，语义清晰、无需全局配置表。
- **匹配规则**：`startswith(f"{vendor}/")`，前缀后必须带斜杠，避免误伤（如 model 恰好等于 vendor 名称时不做剥离）。
- **兼容性**：vendor 为空 = 不剥离 = 完全保持 v2.1 行为；旧 config.json 无 vendor 字段自动补空，无需迁移。
- **不做**：不改写上游响应中的 model 字段（客户端按请求 ID 关联，不影响功能）；不为该特性引入全局开关。
- **性能**：前缀剥离为 O(len(model)) 字符串操作，无额外请求、无缓存逻辑变更，对热路径零影响。

## 架构设计

分层改动，各层职责单一、改动面最小：

```
前端 src/ (vendor 列 + 校验)
  │ IPC 透传 ProxyConfig
  ▼
Rust src-tauri/lib.rs (Endpoint.vendor, #[serde(default)])
  │ 写盘 config.json
  ▼
Python client/config.py (Endpoint.vendor, from_dict 兼容读取)
  │ 运行时 Config
  ▼
Python client/proxy_core.py (chat_completions: 路由到 endpoint → 剥离 model 前缀 → 转发)
```

## 目录结构

```
mimo-proxy/
├── client/
│   └── config.py              # [MODIFY] Endpoint 新增 vendor: str = ""；from_dict 读取 e.get("vendor","")
│   └── proxy_core.py          # [MODIFY] chat_completions 转发前剥离 model 的 vendor 前缀并记录日志
├── src-tauri/src/
│   └── lib.rs                 # [MODIFY] Endpoint 新增 vendor: String（#[serde(default)]）；Default 实现补空串
├── src/
│   ├── index.html             # [MODIFY] 表格表头新增「供应商」列
│   └── app.js                 # [MODIFY] renderTable 新增 vendor 输入框；新增 endpoint 带 vendor:""；保存校验不含 "/"
└── README_CN.md / README.md   # [MODIFY] 配置字段表补充 vendor；新增供应商前缀用法说明
```

## 验证方式

- Python 核心：`tests/mock_upstream.py` 起 mock 上游，curl 发送 `{"model":"lyh/deepseek-v4-flash"}` 到配置了 vendor=lyh 的 endpoint，确认上游收到 `deepseek-v4-flash`；再验证无前缀、前缀不匹配、vendor 为空三种情况原样转发。
- Rust 编译：`cargo check`（或 `npm run build:sidecar`）确认无编译错误。
- 兼容性：用不含 vendor 字段的旧 config.json 启动，确认代理正常读写。