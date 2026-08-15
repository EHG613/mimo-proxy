# mimo-proxy：上游瞬时错误自动重试 + 错误透明化

## 摘要

让 mimo-proxy 吸收中转站（airouter 等 new-api 系网关）的瞬时上游错误：**可重试错误自动退避重试（对 Trae 完全透明），不可恢复错误以真实 HTTP 状态码返回**；同时修复代理自身会"制造"中断的两个缺陷（降级孤儿 tool 消息、错误伪装成 HTTP 200 / 重试重复流）。目标：消除 Trae 经代理使用时"任务异常打断 / The toolcall result is missing"。

## 现状分析（基于当前代码，v2.0 架构）

链路：Trae CN → mimo-proxy（127.0.0.1:8899，`/{name}/v1/chat/completions`）→ 中转站 → 模型上游。

[proxy_core.py](file:///Users/admin/Projects/mimo-proxy/client/proxy_core.py) 当前存在的问题：

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| 1 | 流式错误掩码：非 200 且 <500 时 `yield _sse(error_text)`，对 Trae 是 **HTTP 200 + 一帧无 choices 的数据** | L186-198 | Trae 判定空响应 → 任务中断，且不重试 |
| 2 | 重试不安全：`acc_content/acc_reasoning/acc_tool_calls` 在 attempt 循环外初始化；已转发数据后超时重试会把响应**重复**发给 Trae | L178-183, L259-264 | 流损坏 |
| 3 | 降级孤儿 tool 消息：缓存未命中时删除 `tool_calls` 但保留后续 `role:tool` 消息 | L111-123 | 消息序列非法 → 上游 400 → 走 #1 掩码路径 → 中断 |
| 4 | 非流式路径已正确透传 4xx 并重试 5xx，流式路径未对齐 | L339-393 vs L177-270 | 行为不一致 |
| 5 | 日志仅 stdout，GUI 模式（Tauri spawn 未重定向）事后无法取证 | [__main__.py L19-25](file:///Users/admin/Projects/mimo-proxy/client/__main__.py#L19-L25) | 无法验证/排障 |

中转站错误形态（实测确认 airouter 为 new-api 系）：瞬时上游故障 = 5xx + `Upstream service temporarily unavailable (4028)` 类文案；鉴权类 = 401。

## 方案设计

核心思路：**把"建连 + 状态码检查 + 首帧嗅探"提前到返回 `StreamingResponse` 之前**。此阶段拥有完整的重试控制与真实状态码返回能力；真正的流转发生成器只负责转发与缓存累积。

### 修改 1：错误分类与重试策略（proxy_core.py 新增）

```python
RETRY_MAX_ATTEMPTS = 3          # 总尝试次数
RETRY_BACKOFF_BASE_S = 1.0      # 指数退避 1s / 2s，±30% 随机抖动
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504, 529}
FATAL_STATUSES = {401, 403, 404, 413}
_TRANSIENT_RE = re.compile(
    r"upstream service temporarily unavailable|temporarily unavailable"
    r"|upstream_error|bad gateway|service overloaded|overloaded", re.I)

def _classify_upstream_failure(status: int, body_text: str) -> str:  # "retry" | "fatal"
    # 状态码优先；其次 body 文本匹配（覆盖 new-api 把瞬时错误包进非 5xx 的情况）
    # 默认：5xx→retry，其余 4xx→fatal
```

网络异常（`httpx.TimeoutException` / `ConnectError` / `RemoteProtocolError`）一律 retry。

### 修改 2：流式路径重构（替换 `_stream_proxy` L177-270 与 `chat_completions` L332-337）

**新增 `UpstreamError` 异常**（携带 `status_code` + payload）与 `_open_upstream_stream(client, url, headers, body)`：

1. 用 `client.build_request(...)` + `client.send(req, stream=True)` 发起请求（**不用 `async with client.stream`**，response 需跨函数存活到生成器结束）。
2. 非 200：读 body → `_classify` → retry 则 `await resp.aclose()` 后退避重试；fatal / 耗尽 → 抛 `UpstreamError`（保留上游原始错误体，JSON 则原样、否则包成 `{"error":{"message":..., "code":str(status)}}`）。
3. 200：持续读取直到攒出**首个 `data:` 事件**（跳过 `: comment` 心跳行）。若首帧是 error 形（JSON 含 `error` 键且无 `choices`）→ 按 `_classify` 处理；否则返回 `(resp, first_frame_bytes)`。

**`chat_completions` 流式分支改为：**

```python
try:
    resp, first = await _open_upstream_stream(client, upstream, headers, body)
except UpstreamError as e:
    return JSONResponse(status_code=e.status_code, content=e.payload)
return StreamingResponse(_forward_stream(resp, first, config.cache_max_size), ...)
```

**`_forward_stream(resp, first, max_size)` 生成器：**

- 先 `yield first`，再从 `resp.aiter_bytes()` 续传；`finally: await resp.aclose()`。
- 保留现有 SSE 解析、reasoning/tool_calls 累积、`[DONE]` 缓存逻辑；**累积器在生成器内初始化** → 天然按"成功响应"隔离（修复缺陷 #2）。
- 流中途故障（已向 Trae 转发过数据后超时/断连）：**不再重试**（避免重复内容），记日志，以合法 SSE 收尾（error 帧 + `data: [DONE]`）。

### 修改 3：非流式路径对齐（`chat_completions` L339-393）

- `<500 即透传`的分支改为先 `_classify`：body 命中 transient 模式的 4xx 也进入重试；fatal 4xx 立即透传真实状态码；耗尽 → 502 JSON（保留上游原文）。
- 退避统一用修改 1 的 `_retry_sleep(attempt)` 辅助函数。

### 修改 4：修复降级孤儿 tool 消息（`inject_reasoning` L111-123）

- 缓存未命中分支：**不再删除 `tool_calls`**，改为注入非空占位 `reasoning_content`（如 `"(reasoning unavailable)"`）——MiMo 上游只要求该字段存在于带 tool_calls 的 assistant 消息，消息序列保持合法。
- 日志文案改为 `→ injected placeholder`；返回计数保持 `(injected, degraded)` 结构不变（调用方 L319-321 无需改动）。

### 修改 5：文件日志（`__main__.py` `_setup_logging`）

- 增加 `RotatingFileHandler`：写入配置目录（`MIMO_PROXY_CONFIG_DIR` 优先）下 `logs/proxy.log`，`maxBytes=2MB, backupCount=3`；stdout handler 保留。目的：GUI 模式下重试/降级行为可事后取证。

## 假设与决策

- 重试参数为模块常量，**不进 config.json / 不加 UI**，避免过度设计。
- 不改 Tauri/lib.rs（日志落盘在 Python 侧完成，无需重定向 stdout）。
- 缓存持久化（重启后缓存丢失）不在本次范围。
- 已有模型输出后出现的流内 error 帧：不重试、干净收尾——保证绝不向 Trae 发送重复内容。
- Trae 对真实 4xx/5xx 的展示由 Trae 决定；本方案保证：瞬时错误被吸收、硬错误可见可读。

## 验证步骤

1. 新增 `tests/mock_upstream.py`（测试用 mock 网关）：支持环境变量配置"前 N 次返回指定状态+body，之后返回合法 SSE/JSON"，并提供 `/echo` 回显请求体。
2. 临时环境跑断言：`MIMO_PROXY_CONFIG_DIR=$(mktemp -d)` 写入临时 config（端口 18899，endpoint 指向 mock `http://127.0.0.1:18900/v1`），启动 `.venv/bin/python -m client -v`，curl 验证：
   - A：瞬时 503×2 → SSE 200：代理返回完整流，日志显示 2 次重试成功；
   - B：持续 503：代理返回 **502 JSON**（不再是 200+SSE 掩码）；
   - C：401：立即透传 401，零重试；
   - D：首帧 error 且文案命中 transient：重试后成功；
   - E：`/echo` 验证 cache-miss 消息：`tool_calls` 原样保留 + `reasoning_content` 占位存在（孤儿消息消除）；
   - F：正常流式/非流式回归 + 根路径 `cache_size` 正常增长。
3. 真实验证：重启 sidecar 后 `curl -i` 打 `/airouter/v1/chat/completions`（无鉴权）应得 401 透传；带 key 跑一轮对话确认正常。
4. Trae CN 侧：provider baseURL 指向 `http://127.0.0.1:8899/airouter/v1`，重跑此前会中断的子任务场景，观察 `logs/proxy.log` 的 retry 记录与任务是否顺利完成。

## 涉及文件

| 文件 | 动作 |
|---|---|
| [client/proxy_core.py](file:///Users/admin/Projects/mimo-proxy/client/proxy_core.py) | 修改 1-4（核心） |
| [client/__main__.py](file:///Users/admin/Projects/mimo-proxy/client/__main__.py) | 修改 5（文件日志） |
| tests/mock_upstream.py | 新增（仅测试用） |
