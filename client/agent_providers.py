"""Agent Provider 抽象层：把不同厂商的 Agent SDK 统一成「发一轮 prompt、边收边推文本增量」的接口。

路由层只依赖这里的抽象，不感知具体 SDK。接入新厂商只需新增一个
AgentProvider 子类，并在 `_PROVIDERS` 里注册，无需改动路由与 SSE 转换逻辑。

目前唯一实现是 CodeBuddy（codebuddy-agent-sdk，即 npm `@tencent-ai/agent-sdk`
的 Python 同源包）。
"""

from __future__ import annotations

import logging
import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator, Sequence

log = logging.getLogger("mimo-proxy")


_agent_home_cache: str | None = None


def agent_home() -> str:
    """Agent 独立 HOME：隔离用户个人 rules/skills，避免污染 OpenAI 输出。

    headless CLI 会从 `$HOME/.codebuddy/rules/` 加载用户级规则（作为 memory），
    且无公开开关可禁用。通过把 HOME 指向独立目录，使 agent 与个人 CodeBuddy
    配置完全隔离。

    关键：每个 sidecar 进程使用唯一的子目录（进程内缓存一次）。headless CLI
    会在 `$HOME/.codebuddy/sessions/` 写进程级 session 注册文件，若多个 sidecar
    进程共享同一目录，残留的注册文件会导致多轮会话复用时的 resume 错乱
    （表现为第二轮 receive 空转）。逐进程隔离可彻底避免此问题。
    """
    global _agent_home_cache
    if _agent_home_cache is not None:
        return _agent_home_cache
    override = os.environ.get("MIMO_PROXY_AGENT_HOME")
    if override:
        path = Path(override).expanduser()
    else:
        base = Path.home() / ".mimo-proxy" / "agent-home"
        # 进程内唯一：用 PID + 随机后缀，避免同目录多进程污染
        path = base / f"proc-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    _agent_home_cache = str(path)
    return _agent_home_cache


class AgentError(Exception):
    """Agent 执行失败（含 SDK 未安装 / CLI 未找到 / 连接失败 / 结果非 success）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AgentSession(ABC):
    """一个可复用的多轮会话，内部维护上下文。"""

    @abstractmethod
    def stream(self, prompt: str) -> AsyncIterator[str]:
        """发送一轮提示，边收边推文本增量（打字机效果）。失败抛 AgentError。

        调用方必须完整消费该生成器（或显式 aclose），实现须保证即使
        下游提前断开，本轮剩余消息也会被消费干净，避免残留消息导致
        下一轮会话复用失败。
        """

    async def send(self, prompt: str) -> Sequence[str]:
        """聚合流式增量后一次性返回（供非流式调用方使用）。"""
        return [chunk async for chunk in self.stream(prompt)]

    @abstractmethod
    async def close(self) -> None:
        """释放会话持有的底层资源（CLI 进程等）。"""


class AgentProvider(ABC):
    """厂商 SDK 的工厂抽象。"""

    name: str = ""

    @abstractmethod
    async def create_session(self, model: str | None, cwd: str | None, api_key: str | None) -> AgentSession:
        """按给定模型/工作目录/API key 新建一个会话。

        api_key 由客户端通过 Authorization 头传入，透传给厂商 SDK，实现
        与 OpenAI 一致的「每个调用方用自己的 key」模式。
        """


class CodeBuddySession(AgentSession):
    """CodeBuddy SDK 的多轮会话：一个 client 实例对应一个会话，跨轮次保持上下文。"""

    def __init__(self, client, assistant_cls, text_cls, result_cls, stream_event_cls) -> None:
        self._client = client
        self._assistant_cls = assistant_cls
        self._text_cls = text_cls
        self._result_cls = result_cls
        self._stream_event_cls = stream_event_cls
        self._connected = False

    async def _ensure_connected(self) -> None:
        if not self._connected:
            await self._client.connect()
            self._connected = True

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """边收边推：query 后消费 receive_response()，文本增量即时 yield（打字机效果）。

        依赖 CodeBuddyProvider 开启 `include_partial_messages`：CLI 会以
        StreamEvent（content_block_delta / text_delta）推送 token 级增量，
        此处逐条 yield；随后到达的完整 AssistantMessage 含同一份文本，
        仅作降级兜底（SDK 未开流式增量时输出一次），避免重复。

        断流安全：若下游提前 aclose（GeneratorExit），在排空循环中继续消费
        剩余消息直到 ResultMessage，确保内存队列清空，下一轮复用会话
        不会读到残留（历史「第二轮空转」根因之一）。
        """
        await self._ensure_connected()
        saw_text_delta = False
        try:
            await self._client.query(prompt)
            async for message in self._client.receive_response():
                if isinstance(message, self._stream_event_cls):
                    ev = message.event or {}
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text")
                            if text:
                                saw_text_delta = True
                                yield text
                elif isinstance(message, self._assistant_cls):
                    # 流式增量已推送过时，完整消息冗余；仅在未收到增量时兜底
                    if not saw_text_delta:
                        for block in message.content:
                            if isinstance(block, self._text_cls) and block.text:
                                yield block.text
                elif isinstance(message, self._result_cls):
                    if getattr(message, "subtype", "success") != "success":
                        raise AgentError(
                            f"Agent 执行未成功（subtype={getattr(message, 'subtype', 'unknown')}）"
                        )
        except AgentError:
            raise
        except GeneratorExit:
            # 下游提前断开：不再产出，但把剩余消息消费干净（仅排空，不 yield）
            try:
                async for _ in self._client.receive_response():
                    pass
            except Exception:
                pass
            raise
        except Exception as e:
            # CLI 未找到 / 连接失败 / SDK 内部错误等统一包装，供上层转成 502 中文错误帧
            raise AgentError(f"调用 Agent 失败：{type(e).__name__}: {e}") from e

    async def close(self) -> None:
        try:
            await self._client.disconnect()
        except Exception as e:
            log.debug("CodeBuddy 会话关闭失败（忽略）：%s", e)


class CodeBuddyProvider(AgentProvider):
    name = "codebuddy"

    async def create_session(self, model: str | None, cwd: str | None, api_key: str | None) -> AgentSession:
        # 延迟导入：仅 agent endpoint 才需要 SDK，避免影响纯 HTTP 部署
        try:
            from codebuddy_agent_sdk import (
                AssistantMessage,
                CodeBuddyAgentOptions,
                CodeBuddySDKClient,
                ResultMessage,
                StreamEvent,
                TextBlock,
            )
        except ImportError as e:
            raise AgentError(
                "未安装 codebuddy-agent-sdk，无法使用 agent 类型 endpoint。"
                "请执行 pip install codebuddy-agent-sdk 后重启。"
            ) from e

        options = CodeBuddyAgentOptions()
        if model:
            options.model = model
        if cwd:
            options.cwd = cwd
        # 无人值守的 sidecar 场景下跳过交互式权限确认，语义等价于用户已授权。
        options.permission_mode = "bypassPermissions"
        # 开启流式增量：CLI 以 StreamEvent（content_block_delta/text_delta）
        # 推送 token 级文本，CodeBuddySession.stream() 借此实现打字机效果。
        options.include_partial_messages = True
        # 关键：显式指定唯一 session_id 并关闭磁盘持久化。
        # 默认 persist_session=True 会把会话写盘（agent-home/.codebuddy/sessions/），
        # 且未设 session_id 时 headless 会走 resume 历史重放，导致同一 sidecar
        # 进程内复用会话时第二轮读到上一轮重放或空转。显式隔离后每会话独立、无污染。
        options.session_id = f"mimo-{uuid.uuid4().hex[:12]}"
        options.persist_session = False
        # 环境隔离与认证：
        # - HOME 指向独立目录，隔离用户个人 rules/skills/memory，避免个人规则污染输出；
        # - CODEBUDDY_API_KEY 透传客户端 Authorization 头里的 key（OpenAI 模式）；
        # - CODEBUDDY_INTERNET_ENVIRONMENT 指定网络环境：默认 internal（中国版），
        #   否则 headless 默认走国际版域名，CN 的 ck_ key 会 401。
        options.env = {
            "HOME": agent_home(),
            "CODEBUDDY_API_KEY": api_key or "",
            "CODEBUDDY_INTERNET_ENVIRONMENT": os.environ.get(
                "CODEBUDDY_INTERNET_ENVIRONMENT", "internal"
            ),
        }

        client = CodeBuddySDKClient(options=options)
        session = CodeBuddySession(client, AssistantMessage, TextBlock, ResultMessage, StreamEvent)
        # 关键：在创建 session 时立即 connect，而不是延迟到第一次 send 时。
        # SDK 的 connect 会通过 anyio task group 启动后台 reader（读 CLI stdout）。
        # 若延迟到 uvicorn 的请求 task 里 connect，该请求 task 结束后 reader 可能
        # 被连带取消，导致同一 session 的后续轮次读不到任何消息（第二轮空转）。
        await session._ensure_connected()
        return session


# 已注册的 provider，key 为 endpoint.provider 字段（小写）
_PROVIDERS: dict[str, AgentProvider] = {}


def register_provider(provider: AgentProvider) -> None:
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> AgentProvider:
    """按名称取 provider；未知名称返回 None（由调用方转成清晰错误）。"""
    return _PROVIDERS.get((name or "codebuddy").lower())


register_provider(CodeBuddyProvider())
