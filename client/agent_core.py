"""Agent 会话池与 OpenAI SSE 转换。

职责：
- AgentSessionManager：维护 endpoint → 多轮会话的映射，负责复用、TTL 回收、LRU 淘汰、
  同会话并发串行化。
- 把 AgentSession 产出的文本增量转换为 OpenAI `choices[].delta.content` 帧 dict，
  由 proxy_core 统一序列化为 SSE（`data: {...}`）并追加 `data: [DONE]`。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import AsyncIterator

from .agent_providers import AgentError, AgentSession, get_provider
from .config import Endpoint

log = logging.getLogger("mimo-proxy")

MAX_SESSIONS = 64
SESSION_TTL = 1800.0


@dataclass
class _SessionEntry:
    key: str
    endpoint_name: str
    session: AgentSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.time)


def _normalize_content(content) -> str:
    """把 OpenAI message 的 content 规整为字符串（支持 str 与多模态 list）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def extract_prompt(messages: list[dict]) -> str:
    """从 OpenAI messages 提取本轮发送给 Agent 的 prompt。

    首轮若有 system 消息则前缀拼接；取最后一条 user 消息作为本轮内容。
    """
    system_parts = [
        _normalize_content(m.get("content"))
        for m in messages
        if m.get("role") == "system"
    ]
    system_parts = [p for p in system_parts if p]
    user_msgs = [m for m in messages if m.get("role") == "user"]
    last_user = _normalize_content(user_msgs[-1].get("content")) if user_msgs else ""

    if system_parts:
        return "系统指令：\n" + "\n".join(system_parts) + "\n\n" + last_user
    return last_user


def delta_frame(chat_id: str, model: str, content: str) -> dict:
    """构造一个 OpenAI 流式增量帧。"""
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }
        ],
    }


def done_frame(chat_id: str, model: str) -> dict:
    """流结束前的收尾帧（delta 为空 + finish_reason=stop）。"""
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }


class AgentSessionManager:
    """会话池：按 endpoint+model 维度复用会话，含 TTL 回收与 LRU 淘汰。"""

    def __init__(self, max_sessions: int = MAX_SESSIONS, session_ttl: float = SESSION_TTL) -> None:
        self._sessions: "OrderedDict[str, _SessionEntry]" = OrderedDict()
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl
        self._lock = asyncio.Lock()

    def _key(self, endpoint_name: str, model: str | None, api_key: str | None) -> str:
        # api_key 取短哈希，既区分不同调用方的会话，又不把完整 key 写进日志
        key_digest = hashlib.sha256((api_key or "").encode()).hexdigest()[:8]
        return f"{endpoint_name}:{model or 'default'}:{key_digest}"

    async def acquire(self, endpoint: Endpoint, model: str | None, api_key: str | None) -> _SessionEntry:
        key = self._key(endpoint.name, model, api_key)
        async with self._lock:
            entry = self._sessions.get(key)
            now = time.time()
            if entry is not None:
                if now - entry.last_used < self._session_ttl:
                    entry.last_used = now
                    self._sessions.move_to_end(key)
                    return entry
                # 过期：关闭并移除
                self._sessions.pop(key, None)
                await self._close_entry(entry)

            # 新建会话
            provider = get_provider(endpoint.provider)
            if provider is None:
                raise AgentError(f"未知的 agent provider: '{endpoint.provider}'")
            session = await provider.create_session(model, None, api_key)

            # LRU 淘汰：超限时关闭最久未用
            while len(self._sessions) >= self._max_sessions:
                _, oldest = self._sessions.popitem(last=False)
                await self._close_entry(oldest)

            entry = _SessionEntry(key=key, endpoint_name=endpoint.name, session=session)
            self._sessions[key] = entry
            log.info("🤖 新建 agent 会话 [%s] (provider=%s)", key, endpoint.provider)
            return entry

    async def _close_entry(self, entry: _SessionEntry) -> None:
        try:
            await entry.session.close()
        except Exception as e:
            log.debug("会话关闭失败（忽略）：%s", e)

    async def stream(
        self,
        entry: _SessionEntry,
        prompt: str,
        model: str | None,
        chat_id: str,
    ) -> AsyncIterator[dict]:
        """在同一会话内串行发送 prompt，逐块产出 OpenAI delta 帧 dict。

        同一 session 的并发请求通过 entry.lock 排队，避免 CLI 会话交错；
        不同 session 由各自 lock 互不阻塞，天然并行。

        session.stream() 边收边推（打字机效果）：文本增量到达即转成
        SSE delta 帧转发给下游；即使下游提前断开，session.stream 内部
        也会把本轮剩余消息排空，会话状态不会残留。
        """
        model_name = model or "agent"
        async with entry.lock:
            async for text in entry.session.stream(prompt):
                yield delta_frame(chat_id, model_name, text)

    async def close_all(self) -> None:
        async with self._lock:
            entries = list(self._sessions.values())
            self._sessions.clear()
        for entry in entries:
            await self._close_entry(entry)


_manager: AgentSessionManager | None = None


def get_session_manager() -> AgentSessionManager:
    global _manager
    if _manager is None:
        _manager = AgentSessionManager()
    return _manager


def new_chat_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"
