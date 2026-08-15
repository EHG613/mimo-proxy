"""测试用 mock 上游网关：模拟 new-api 系中转站的瞬时错误行为。

用法示例::

    FAIL_FIRST=2 FAIL_STATUS=503 PORT=18900 python tests/mock_upstream.py

环境变量：
    PORT              监听端口（默认 18900）
    FAIL_FIRST        前 N 次 /v1/chat/completions 请求返回失败（默认 0）
    FAIL_STATUS       失败状态码（默认 503）
    FAIL_BODY         失败响应体（默认模拟 new-api 瞬时错误文案）
    FIRST_FRAME_ERROR 非零时失败请求返回 200 + SSE 首帧 error（测试首帧嗅探重试）
    STRICT_REASONING  非零时校验：带 tool_calls 的 assistant 消息必须携带非空
                      reasoning_content，否则 400（模拟 MiMo 上游约束）
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "18900"))
FAIL_FIRST = int(os.environ.get("FAIL_FIRST", "0"))
FAIL_STATUS = int(os.environ.get("FAIL_STATUS", "503"))
FAIL_BODY = os.environ.get(
    "FAIL_BODY",
    '{"error":{"message":"Upstream service temporarily unavailable (4028)","type":"new_api_error"}}',
)
FIRST_FRAME_ERROR = bool(int(os.environ.get("FIRST_FRAME_ERROR", "0")))
STRICT_REASONING = bool(int(os.environ.get("STRICT_REASONING", "0")))

_lock = threading.Lock()
_hits = 0

_CHUNK = {
    "id": "chatcmpl-mock",
    "object": "chat.completion.chunk",
    "created": 0,
    "model": "mock",
}


def _sse(payload: dict | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静模式
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        global _hits
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"bad json"}')
            return

        if self.path.endswith("/chat/completions"):
            if STRICT_REASONING:
                for msg in body.get("messages", []):
                    if msg.get("role") == "assistant" and msg.get("tool_calls") \
                            and not msg.get("reasoning_content"):
                        self._send(400, json.dumps({
                            "error": {"message": "assistant message with tool_calls "
                                                 "must contain reasoning_content",
                                      "type": "invalid_request_error"},
                        }).encode())
                        return

            with _lock:
                _hits += 1
                hit = _hits

            if hit <= FAIL_FIRST:
                if FIRST_FRAME_ERROR:
                    self._send(200, _sse({"error": {"message": "Upstream service "
                                                               "temporarily unavailable (4028)",
                                                    "type": "upstream_error"}})
                               + _sse("[DONE]"), "text/event-stream")
                else:
                    self._send(FAIL_STATUS, FAIL_BODY.encode())
                return

            if body.get("stream"):
                out = b""
                delta_role = dict(_CHUNK, choices=[{"index": 0, "delta": {
                    "role": "assistant", "reasoning_content": "mock thinking"}, "finish_reason": None}])
                delta_text = dict(_CHUNK, choices=[{"index": 0, "delta": {
                    "content": "hello"}, "finish_reason": None}])
                delta_stop = dict(_CHUNK, choices=[{"index": 0, "delta": {},
                                                    "finish_reason": "stop"}])
                out += _sse(delta_role) + _sse(delta_text) + _sse(delta_stop) + _sse("[DONE]")
                self._send(200, out, "text/event-stream")
            else:
                self._send(200, json.dumps({
                    "id": "chatcmpl-mock", "object": "chat.completion", "created": 0,
                    "model": "mock",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {
                        "role": "assistant", "content": "hello",
                        "reasoning_content": "mock thinking"}}],
                }).encode())
            return

        self._send(404, b'{"error":"not found"}')


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock upstream on http://127.0.0.1:{PORT} "
          f"(FAIL_FIRST={FAIL_FIRST}, FAIL_STATUS={FAIL_STATUS}, "
          f"FIRST_FRAME_ERROR={FIRST_FRAME_ERROR}, STRICT_REASONING={STRICT_REASONING})")
    server.serve_forever()
