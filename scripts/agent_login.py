"""在独立 HOME 下完成 CodeBuddy Agent 登录，隔离用户个人 rules/skills。

agent 类型的 endpoint 使用独立 HOME（默认 ~/.mimo-proxy/agent-home），
需在此目录下单独登录一次。登录态过期后重新执行本脚本即可。

用法：
    .venv/bin/python scripts/agent_login.py
"""

from __future__ import annotations

import asyncio
import sys
import webbrowser

from codebuddy_agent_sdk import authenticate

from client.agent_providers import agent_home


async def main() -> int:
    home = agent_home()
    print(f"Agent HOME: {home}", flush=True)

    auth = await authenticate(env={"HOME": home})
    if not auth.auth_url:
        print("已登录，无需重复操作。", flush=True)
        return 0

    print(f"登录 URL: {auth.auth_url}", flush=True)
    try:
        webbrowser.open(auth.auth_url)
    except Exception:
        pass
    print("请在浏览器中完成登录…", flush=True)

    try:
        result = await auth
        print(f"登录成功: {result.userinfo.user_name}", flush=True)
        return 0
    except Exception as e:
        print(f"登录失败: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
