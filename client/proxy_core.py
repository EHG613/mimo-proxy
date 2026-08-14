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
import hashlib
import json
import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Iterable

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .config import Config, Endpoint

log = logging.getLogger("mimo-proxy")

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
    """处理 assistant 消息：有缓存则注入 reasoning_content，无缓存则剥离 tool_calls 降级。"""
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
            log.warning("⚠️  No cache for msg[%d] [%s] tool_call_ids=%s → degrading to plain text",
                        i, h[:8], tc_ids)
            original_content = msg.get("content") or ""
            tc_summary = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                tc_summary.append(f"[Called {fn.get('name', '?')}]")
            if tc_summary:
                msg["content"] = original_content + " " + " ".join(tc_summary)
            del msg["tool_calls"]
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


# ─── SSE 流式处理 ──────────────────────────────────────────────

def _sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode("utf-8")


async def _stream_proxy(client: httpx.AsyncClient, url: str, headers: dict, body: dict, max_size: int):
    acc_content = ""
    acc_reasoning = ""
    acc_tool_calls: list[dict] = []

    last_error = None
    for attempt in range(3):
        try:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_text = error_body.decode("utf-8", errors="replace")
                    log.warning("⚠️ Stream upstream %d (attempt %d): %s", resp.status_code, attempt + 1, error_text[:200])
                    if resp.status_code < 500:
                        yield _sse(error_text)
                        return
                    last_error = error_text
                    if attempt < 2:
                        await asyncio.sleep(1 * (attempt + 1))
                        continue
                    yield _sse(json.dumps({"error": {"message": f"MiMo API error after retries: {last_error[:200]}", "code": "502"}}))
                    return

                buffer = ""
                async for raw_chunk in resp.aiter_bytes():
                    buffer += raw_chunk.decode("utf-8", errors="replace")

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
                return

        except httpx.TimeoutException as e:
            log.warning("⚠️ Stream timeout (attempt %d): %s", attempt + 1, e)
            last_error = str(e)
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
        except Exception as e:
            log.error("❌ Stream error: %s", e, exc_info=True)
            yield _sse(json.dumps({"error": f"Proxy error: {e}"}))
            return

    yield _sse(json.dumps({"error": {"message": f"Stream error after retries: {last_error}", "code": "502"}}))


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

    headers = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["authorization"] = auth

    is_stream = body.get("stream", False)
    upstream = f"{endpoint.base_url}/chat/completions"
    client = await state.get_client()

    if is_stream:
        return StreamingResponse(
            _stream_proxy(client, upstream, headers, body, config.cache_max_size),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    last_error = None
    for attempt in range(3):
        try:
            resp = await client.post(upstream, headers=headers, json=body)
            if resp.status_code != 200:
                error_text = resp.text
                log.warning("⚠️ [%s] Upstream %d (attempt %d): %s",
                            endpoint.name, resp.status_code, attempt + 1, error_text[:200])
                if resp.status_code < 500:
                    return JSONResponse(
                        {"error": {"message": f"Upstream error: {error_text[:200]}", "code": str(resp.status_code)}},
                        status_code=resp.status_code,
                    )
                last_error = error_text
                if attempt < 2:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                return JSONResponse(
                    {"error": {"message": f"MiMo API error after 3 attempts: {last_error[:200]}", "code": "502"}},
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

        except httpx.TimeoutException as e:
            log.warning("⚠️ [%s] Timeout (attempt %d): %s", endpoint.name, attempt + 1, e)
            last_error = str(e)
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
        except Exception as e:
            log.error("❌ [%s] Error: %s", endpoint.name, e, exc_info=True)
            return JSONResponse({"error": {"message": str(e), "code": "500"}}, status_code=500)

    return JSONResponse(
        {"error": {"message": f"Proxy error after retries: {last_error}", "code": "502"}},
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
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


async def root(request: Request):
    config = _state.config
    return JSONResponse({
        "status": "running",
        "service": "MiMo Reasoning Content Proxy v2.0",
        "host": config.host,
        "port": config.port,
        "default_name": config.default_name,
        "endpoints": [
            {"name": e.name, "base_url": e.base_url, "enabled": e.enabled, "is_default": e.name == config.default_name}
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
