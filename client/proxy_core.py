"""MiMo Reasoning Content Proxy 核心：拦截响应缓存 reasoning_content，请求时注入。

相比原 mimo_proxy.py，本模块支持多个上游 baseURL，通过路径前缀路由：

    /{name}/v1/chat/completions   →  该 name 对应的 baseURL
    /{name}/v1/models             →  该 name 对应的 /models
    /v1/chat/completions          →  默认 endpoint（向后兼容）
    /v1/models                    →  默认 endpoint
    /                             →  状态页（列出所有 endpoint）

共享缓存（按 message content+tool_calls 哈希），跨 endpoint 复用。
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
import random
import re
import socket
import ssl
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import AsyncIterator, Iterable

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .config import Config, Endpoint

log = logging.getLogger("mimo-proxy")

# ─── 上游错误分类与重试策略 ────────────────────────────────────
# 目标：吸收中转站（new-api 系网关）的瞬时上游错误，对客户端透明；
# 不可恢复错误以真实 HTTP 状态码返回，避免客户端把错误流当成空响应。

RETRY_MAX_ATTEMPTS = 3          # 总尝试次数
RETRY_BACKOFF_BASE_S = 1.0      # 指数退避基数：1s / 2s，±30% 随机抖动
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504, 529}
FATAL_STATUSES = {401, 403, 404, 413}
_TRANSIENT_RE = re.compile(
    r"upstream service temporarily unavailable|temporarily unavailable"
    r"|upstream_error|bad gateway|service overloaded|overloaded",
    re.IGNORECASE,
)


def _classify_upstream_failure(status: int, body_text: str) -> str:
    """判断上游失败类型：retry（瞬时，可重试）或 fatal（不可恢复）。"""
    if status in RETRYABLE_STATUSES:
        return "retry"
    # 覆盖 new-api 把瞬时上游错误包进 4xx 的情况（401/403/404 除外，明确不可重试）
    if status not in FATAL_STATUSES and _TRANSIENT_RE.search(body_text or ""):
        return "retry"
    if status >= 500:
        return "retry"
    return "fatal"


def _describe_error(e: Exception) -> str:
    """把底层网络异常映射为用户可读的中文说明，便于快速定位失败/中断原因。"""
    if isinstance(e, httpx.ConnectTimeout):
        return "连接上游服务器超时（超过30s）：服务器无响应、网络不通或端口不可达"
    if isinstance(e, httpx.PoolTimeout):
        return "上游连接池已满，排队等待连接超时（超过300s）：并发请求过多"
    if isinstance(e, httpx.ReadTimeout):
        return "等待上游响应超时（超过300s）：上游处理过慢或已无响应"
    if isinstance(e, httpx.WriteTimeout):
        return "向上游发送请求超时（超过300s）"
    if isinstance(e, httpx.RemoteProtocolError):
        return f"上游在返回响应前断开了连接：网关重启、过载或中间链路被中断（{e}）"
    if isinstance(e, httpx.ConnectError):
        cause = e.__cause__
        if isinstance(cause, socket.gaierror):
            return f"无法解析上游域名（DNS 解析失败：{cause}）"
        if isinstance(cause, ConnectionRefusedError):
            return "上游连接被拒绝：服务未启动或端口未监听"
        if isinstance(cause, ssl.SSLError):
            return f"与上游 TLS/SSL 握手失败或证书校验不通过（{cause}）"
        return f"连接上游失败（{cause or e}）"
    if isinstance(e, httpx.ReadError):
        return f"读取上游响应中断：上游进程崩溃或网络断开（{e}）"
    return f"{type(e).__name__}: {e}"


class UpstreamError(Exception):
    """上游不可恢复错误：携带真实状态码与错误 payload，由端点转成 JSONResponse。"""

    def __init__(self, status_code: int, payload: dict):
        super().__init__(f"upstream {status_code}")
        self.status_code = status_code
        self.payload = payload


def _error_payload(status: int, body_text: str) -> dict:
    """把上游错误体规整成 OpenAI 风格 payload；JSON 错误体原样保留。"""
    try:
        data = json.loads(body_text)
        if isinstance(data, dict) and "error" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return {"error": {"message": (body_text or "")[:500] or f"upstream {status}", "code": str(status)}}


async def _retry_sleep(attempt: int) -> None:
    """attempt 从 0 开始：第 1 次重试前睡 ~1s，第 2 次前睡 ~2s，带 ±30% 抖动。"""
    delay = RETRY_BACKOFF_BASE_S * (2 ** attempt) * random.uniform(0.7, 1.3)
    log.info("🔁 retrying in %.1fs (attempt %d/%d)", delay, attempt + 2, RETRY_MAX_ATTEMPTS)
    await asyncio.sleep(delay)


async def _give_up_or_retry(resp: httpx.Response | None, status: int, body_text: str, attempt: int) -> None:
    """关闭上游响应；瞬时错误且还有次数时退避等待（返回后由调用方重试），否则抛 UpstreamError。"""
    if resp is not None:
        await resp.aclose()
    if _classify_upstream_failure(status, body_text) == "retry" and attempt < RETRY_MAX_ATTEMPTS - 1:
        await _retry_sleep(attempt)
        return
    raise UpstreamError(status, _error_payload(status, body_text))

# ─── 共享缓存 ──────────────────────────────────────────────────
_cache: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_tool_call_index: dict[str, str] = {}


def _msg_hash(msg: dict) -> str:
    content = msg.get("content") or ""
    tool_calls = json.dumps(msg.get("tool_calls") or [], sort_keys=True, ensure_ascii=False)
    raw = f"{content}||{tool_calls}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _extract_tool_call_ids(msg: dict) -> list[str]:
    return [tc.get("id", "") for tc in msg.get("tool_calls") or [] if tc.get("id")]


def _cache_get(key: str, ttl: int) -> str | None:
    if key in _cache:
        val, ts = _cache[key]
        if time.time() - ts < ttl:
            _cache.move_to_end(key)
            return val
        del _cache[key]
    return None


def _cache_set(key: str, value: str, max_size: int) -> None:
    if key in _cache:
        del _cache[key]
    _cache[key] = (value, time.time())
    while len(_cache) > max_size:
        _cache.popitem(last=False)


def _cache_set_with_index(key: str, value: str, tool_call_ids: list[str], max_size: int) -> None:
    _cache_set(key, value, max_size)
    for tid in tool_call_ids:
        _tool_call_index[tid] = value


def _find_by_tool_call_ids(msg: dict) -> str | None:
    for tid in _extract_tool_call_ids(msg):
        if tid in _tool_call_index:
            return _tool_call_index[tid]
    return None


def cache_stats() -> dict:
    return {
        "cache_size": len(_cache),
        "tool_call_index_size": len(_tool_call_index),
    }


# ─── 核心逻辑 ──────────────────────────────────────────────────

def inject_reasoning(messages: list[dict], ttl: int, max_size: int) -> tuple[int, int]:
    """处理 assistant 消息：有缓存则注入 reasoning_content，无缓存则注入占位符。

    注意：始终保留 tool_calls——删除 tool_calls 会留下孤儿 role:tool 消息，
    导致上游返回 400（消息序列非法）。占位 reasoning_content 同样满足
    MiMo 上游"带 tool_calls 的 assistant 消息必须携带 reasoning_content"的约束。
    """
    injected = 0
    degraded = 0

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        if not msg.get("tool_calls"):
            continue
        if msg.get("reasoning_content"):
            continue

        h = _msg_hash(msg)
        cached = _cache_get(h, ttl) or _find_by_tool_call_ids(msg)

        if cached:
            msg["reasoning_content"] = cached
            injected += 1
            log.info("✅ Injected reasoning_content into msg[%d] [%s] (%d chars)", i, h[:8], len(cached))
        else:
            tc_ids = _extract_tool_call_ids(msg)
            log.warning("⚠️  No cache for msg[%d] [%s] tool_call_ids=%s → injected placeholder reasoning_content",
                        i, h[:8], tc_ids)
            msg["reasoning_content"] = "(reasoning unavailable)"
            degraded += 1

    return injected, degraded


def cache_reasoning_from_message(msg: dict, max_size: int) -> None:
    rc = msg.get("reasoning_content")
    if rc and msg.get("tool_calls"):
        h = _msg_hash(msg)
        tc_ids = _extract_tool_call_ids(msg)
        _cache_set_with_index(h, rc, tc_ids, max_size)
        log.info("📦 Cached reasoning [%s] (%d chars) tc_ids=%s", h[:8], len(rc), tc_ids)


# ─── 路径解析 ──────────────────────────────────────────────────

# 这些路径段不能作为 endpoint 名称（保留字）
_RESERVED_PREFIXES = {"v1", "models", "chat", "health", ""}


def resolve_endpoint(config: Config, path: str) -> tuple[Endpoint | None, str, str | None]:
    """根据请求路径解析出 endpoint 和剩余路径。

    返回 (endpoint, remaining_path, error_message)。
    - 命中 /{name}/...：返回该 endpoint 和 /...
    - 未命中前缀但匹配默认路由（/v1/...）：返回默认 endpoint
    - 路径不合法：返回 (None, path, error)
    """
    stripped = path.strip("/")
    if not stripped:
        return config.default_endpoint(), "/", None

    parts = stripped.split("/", 1)
    if len(parts) == 2:
        candidate = parts[0]
        if candidate not in _RESERVED_PREFIXES:
            ep = config.find_endpoint(candidate)
            if ep:
                return ep, "/" + parts[1], None
            return None, path, f"Unknown endpoint '{candidate}'"

    # 落到默认 endpoint
    default_ep = config.default_endpoint()
    if default_ep is None:
        return None, path, "No endpoint configured"
    return default_ep, path, None


def strip_model_vendor(model: str, vendor: str) -> tuple[str, bool]:
    """按 endpoint 的 vendor 剥离模型 id 前缀。

    客户端以「供应商/模型id」配置模型（如 lyh/deepseek-v4-flash）来避开
    与工具内置模型的 id 冲突；转发前还原为真实模型 id（deepseek-v4-flash）。

    返回 (真实模型 id, 是否发生剥离)。vendor 为空或不匹配前缀时原样返回。
    """
    if vendor and model.startswith(f"{vendor}/"):
        return model[len(vendor) + 1:], True
    return model, False


# ─── SSE 流式处理 ──────────────────────────────────────────────

def _sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode("utf-8")


def _first_frame_error_text(event_text: str) -> str | None:
    """若首个 SSE 事件的 data 是 error 帧（JSON 含 error 键且无 choices），返回其原文，否则 None。"""
    for line in event_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if isinstance(chunk, dict) and "error" in chunk and "choices" not in chunk:
            return json.dumps(chunk, ensure_ascii=False)
        return None
    return None


async def _open_upstream_stream(
    client: httpx.AsyncClient, url: str, headers: dict, body: dict,
) -> tuple[httpx.Response, AsyncIterator[bytes], bytes]:
    """建立上游流：状态码检查 + 首帧嗅探 + 瞬时错误退避重试。

    在向客户端写出任何字节之前完成，因此可以：
    - 对可重试错误（5xx/429/网络异常/流内首帧 error）自动重试，对客户端透明；
    - 对不可恢复错误抛 UpstreamError（真实状态码），由端点返回 JSONResponse。

    返回 (仍处于流式打开状态的 response, 可续传的字节迭代器, 已预读的首段字节)；
    response 与迭代器的关闭由 _forward_stream 负责。
    注意：httpx 的 aiter_bytes() 只能消费一次，因此预读与续传必须共用同一迭代器对象。
    """
    for attempt in range(RETRY_MAX_ATTEMPTS):
        resp: httpx.Response | None = None
        try:
            req = client.build_request("POST", url, headers=headers, json=body)
            resp = await client.send(req, stream=True)

            if resp.status_code != 200:
                error_text = (await resp.aread()).decode("utf-8", errors="replace")
                log.warning("⚠️ Stream upstream %d (attempt %d/%d): %s",
                            resp.status_code, attempt + 1, RETRY_MAX_ATTEMPTS, error_text[:200])
                await _give_up_or_retry(resp, resp.status_code, error_text, attempt)
                continue

            # 200：预读首个完整 SSE 事件，嗅探流内 error 帧
            byte_iter = resp.aiter_bytes()
            buf = b""
            got_event = False
            async for raw in byte_iter:
                buf += raw
                if b"\n\n" in buf or b"[DONE]" in buf:
                    got_event = True
                    break
            if not got_event:
                log.warning("⚠️ Stream upstream empty (attempt %d/%d)", attempt + 1, RETRY_MAX_ATTEMPTS)
                await _give_up_or_retry(resp, 502, "upstream returned empty stream", attempt)
                continue

            err_text = _first_frame_error_text(buf.decode("utf-8", errors="replace"))
            if err_text is not None:
                log.warning("⚠️ Stream first-frame error (attempt %d/%d): %s",
                            attempt + 1, RETRY_MAX_ATTEMPTS, err_text[:200])
                await _give_up_or_retry(resp, 502, err_text, attempt)
                continue

            return resp, byte_iter, buf

        except UpstreamError:
            raise
        except httpx.RequestError as e:
            log.warning("⚠️ Stream network error (attempt %d/%d): %s", attempt + 1, RETRY_MAX_ATTEMPTS, e)
            if resp is not None:
                await resp.aclose()
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                await _retry_sleep(attempt)
                continue
            raise UpstreamError(
                502,
                {"error": {"message": f"连接上游失败（已重试{RETRY_MAX_ATTEMPTS}次）：{_describe_error(e)}", "code": "502"}},
            )

    raise UpstreamError(502, {"error": {"message": "上游不可用", "code": "502"}})


async def _forward_stream(resp: httpx.Response, byte_iter: AsyncIterator[bytes], first: bytes, max_size: int):
    """转发上游 SSE 流：累积 reasoning/tool_calls 并在 [DONE] 时写缓存。

    首段字节 first 由 _open_upstream_stream 预读传入（保证重试判断发生在
    写出之前）；累积器在生成器内初始化，天然按"成功响应"隔离。
    流中途故障（已向客户端转发过数据）不重试，以合法 SSE 收尾，避免内容重复。
    """
    acc_content = ""
    acc_reasoning = ""
    acc_tool_calls: list[dict] = []

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""

    async def _chunks():
        # 预读的首段作为第一个数据块，续传共用同一迭代器（httpx 限制只能消费一次）
        if first:
            yield first
        async for raw in byte_iter:
            yield raw

    try:
        async for raw_chunk in _chunks():
            buffer += decoder.decode(raw_chunk)

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")

                if line.startswith("data: "):
                    payload = line[6:].strip()

                    if payload == "[DONE]":
                        if acc_reasoning and (acc_content or acc_tool_calls):
                            synthetic = {
                                "role": "assistant",
                                "content": acc_content,
                                "tool_calls": acc_tool_calls,
                                "reasoning_content": acc_reasoning,
                            }
                            h = _msg_hash(synthetic)
                            tc_ids = _extract_tool_call_ids(synthetic)
                            _cache_set_with_index(h, acc_reasoning, tc_ids, max_size)
                            log.info("📦 Cached streaming reasoning [%s] (%d chars)", h[:8], len(acc_reasoning))
                        yield _sse("[DONE]")
                        continue

                    try:
                        chunk = json.loads(payload)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        rc = delta.get("reasoning_content")
                        if rc:
                            acc_reasoning += rc
                        c = delta.get("content")
                        if c:
                            acc_content += c
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            while len(acc_tool_calls) <= idx:
                                acc_tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if tc.get("id"):
                                acc_tool_calls[idx]["id"] = tc["id"]
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                acc_tool_calls[idx]["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                acc_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                    except (json.JSONDecodeError, IndexError, KeyError):
                        pass

                    yield _sse(payload)

                elif line.strip() == "":
                    yield b"\n"
                elif line.startswith(":"):
                    yield (line + "\n\n").encode("utf-8")
                else:
                    yield (line + "\n").encode("utf-8")

    except httpx.RequestError as e:
        # 已向客户端转发过数据 → 不能重试（会造成内容重复），干净收尾
        log.error("❌ Stream aborted mid-flight: %s", e)
        yield _sse(json.dumps({"error": {"message": f"上游流中途中断：{_describe_error(e)}", "code": "502"}}))
        yield _sse("[DONE]")
    except Exception as e:
        log.error("❌ Stream error: %s", e, exc_info=True)
        yield _sse(json.dumps({"error": {"message": f"代理内部错误：{e}", "code": "500"}}))
        yield _sse("[DONE]")
    finally:
        await resp.aclose()


# ─── HTTP 端点 ─────────────────────────────────────────────────

class ProxyState:
    """运行时状态：当前配置 + httpx 客户端。"""

    def __init__(self) -> None:
        self.config: Config = Config()
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    def update_config(self, config: Config) -> None:
        self.config = config

    async def get_client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(300, connect=30),
                    follow_redirects=True,
                )
            return self._client


_state = ProxyState()


def get_state() -> ProxyState:
    return _state


async def chat_completions(request: Request):
    state = _state
    config = state.config

    endpoint, remaining, err = resolve_endpoint(config, request.url.path)
    if err or endpoint is None:
        return JSONResponse({"error": err or "No endpoint"}, status_code=503)
    if not endpoint.enabled:
        return JSONResponse({"error": f"Endpoint '{endpoint.name}' is disabled"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    messages = body.get("messages", [])
    injected, degraded = inject_reasoning(messages, config.cache_ttl, config.cache_max_size)
    if injected or degraded:
        log.info("🔧 [%s] Injected=%d, Degraded=%d", endpoint.name, injected, degraded)

    # 供应商前缀剥离：客户端可配「vendor/模型id」避免与工具内置模型冲突，转发前还原真实模型 id
    model = body.get("model", "")
    if model:
        real_model, stripped = strip_model_vendor(str(model), endpoint.vendor)
        if stripped:
            log.info("🏷️  [%s] Model '%s' → '%s' (stripped vendor '%s')",
                     endpoint.name, model, real_model, endpoint.vendor)
            body["model"] = real_model

    headers = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["authorization"] = auth

    is_stream = body.get("stream", False)
    upstream = f"{endpoint.base_url}/chat/completions"
    client = await state.get_client()

    if is_stream:
        try:
            resp, byte_iter, first = await _open_upstream_stream(client, upstream, headers, body)
        except UpstreamError as e:
            log.warning("🚫 [%s] Stream upstream failed: %d %s", endpoint.name, e.status_code, str(e.payload)[:200])
            return JSONResponse(status_code=e.status_code, content=e.payload)
        return StreamingResponse(
            _forward_stream(resp, byte_iter, first, config.cache_max_size),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    last_error = None
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            resp = await client.post(upstream, headers=headers, json=body)
            if resp.status_code != 200:
                error_text = resp.text
                log.warning("⚠️ [%s] Upstream %d (attempt %d/%d): %s",
                            endpoint.name, resp.status_code, attempt + 1, RETRY_MAX_ATTEMPTS, error_text[:200])
                verdict = _classify_upstream_failure(resp.status_code, error_text)
                if verdict == "retry" and attempt < RETRY_MAX_ATTEMPTS - 1:
                    await _retry_sleep(attempt)
                    continue
                if verdict == "fatal":
                    return JSONResponse(content=_error_payload(resp.status_code, error_text), status_code=resp.status_code)
                return JSONResponse(
                    {"error": {"message": f"Upstream error after {RETRY_MAX_ATTEMPTS} attempts: {error_text[:200]}", "code": "502"}},
                    status_code=502,
                )

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                log.warning("⚠️ [%s] Empty choices in response", endpoint.name)
                return JSONResponse(
                    {"error": {"message": "MiMo API returned empty choices", "code": "502"}},
                    status_code=502,
                )

            msg = choices[0].get("message", {})
            if not msg.get("content") and not msg.get("tool_calls") and msg.get("reasoning_content"):
                log.warning("⚠️ [%s] Response has reasoning_content but no content, setting fallback", endpoint.name)
                msg["content"] = msg["reasoning_content"]

            for choice in choices:
                cache_reasoning_from_message(choice.get("message", {}), config.cache_max_size)

            return JSONResponse(content=data, status_code=200)

        except httpx.RequestError as e:
            log.warning("⚠️ [%s] Upstream network error (attempt %d/%d): %s",
                        endpoint.name, attempt + 1, RETRY_MAX_ATTEMPTS, e)
            last_error = _describe_error(e)
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                await _retry_sleep(attempt)
                continue
        except Exception as e:
            log.error("❌ [%s] Error: %s", endpoint.name, e, exc_info=True)
            return JSONResponse({"error": {"message": f"代理内部错误：{e}", "code": "500"}}, status_code=500)

    return JSONResponse(
        {"error": {"message": f"代理请求上游失败（已重试{RETRY_MAX_ATTEMPTS}次）：{last_error}", "code": "502"}},
        status_code=502,
    )


async def list_models(request: Request):
    state = _state
    config = state.config

    endpoint, remaining, err = resolve_endpoint(config, request.url.path)
    if err or endpoint is None:
        return JSONResponse({"error": err or "No endpoint"}, status_code=503)
    if not endpoint.enabled:
        return JSONResponse({"error": f"Endpoint '{endpoint.name}' is disabled"}, status_code=503)

    headers = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["authorization"] = auth
    client = await state.get_client()
    try:
        resp = await client.get(f"{endpoint.base_url}/models", headers=headers)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.RequestError as e:
        log.warning("⚠️ [%s] Models network error: %s", endpoint.name, e)
        return JSONResponse(
            {"error": {"message": f"获取模型列表失败：{_describe_error(e)}", "code": "502"}},
            status_code=502,
        )
    except Exception as e:
        return JSONResponse({"error": f"获取模型列表失败：{e}"}, status_code=502)


async def root(request: Request):
    config = _state.config
    return JSONResponse({
        "status": "running",
        "service": "MiMo Reasoning Content Proxy v2.1.2",
        "host": config.host,
        "port": config.port,
        "default_name": config.default_name,
        "endpoints": [
            {"name": e.name, "base_url": e.base_url, "enabled": e.enabled, "vendor": e.vendor,
             "is_default": e.name == config.default_name}
            for e in config.endpoints
        ],
        "routes_hint": "Use /{name}/v1/chat/completions to target a specific endpoint, or /v1/chat/completions for the default.",
        **cache_stats(),
    })


async def health(request: Request):
    return JSONResponse({"ok": True})


# ─── Starlette App 工厂 ────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    log.info("🚀 httpx client initialized")
    yield
    client = _state._client
    if client:
        await client.aclose()
        _state._client = None


def create_app(config: Config) -> Starlette:
    """根据 config 构建 Starlette app。注意：config 后续可通过 update_config 动态更新。"""
    _state.update_config(config)
    routes = [
        Route("/", root),
        Route("/health", health),
        # 默认路由（不带 endpoint 前缀，使用 default endpoint）
        Route("/v1/models", list_models),
        Route("/models", list_models),
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/chat/completions", chat_completions, methods=["POST"]),
        # 带 endpoint 前缀：用 catch-all 匹配 /{name}/...
        Route("/{_name}/v1/models", list_models),
        Route("/{_name}/models", list_models),
        Route("/{_name}/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/{_name}/chat/completions", chat_completions, methods=["POST"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
