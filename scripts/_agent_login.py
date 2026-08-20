"""临时脚本：完成 headless CLI 的 OAuth 登录，登录态持久化后 agent endpoint 即可用。"""
import asyncio
import sys

from codebuddy_agent_sdk import authenticate


async def main() -> int:
    auth = await authenticate()
    if not auth.auth_url:
        print("ALREADY_AUTH", flush=True)
        return 0
    print(f"AUTH_URL:{auth.auth_url}", flush=True)
    try:
        result = await auth
        print(f"LOGIN_OK:{result.userinfo.user_name}", flush=True)
        return 0
    except Exception as e:
        print(f"LOGIN_FAIL:{type(e).__name__}:{e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
