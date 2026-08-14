"""MiMo Proxy 客户端入口。

Tauri 2 桌面应用通过 ``python -m client --cli`` 启动代理 sidecar。
也可以直接 ``python -m client`` 启动纯命令行代理。
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .config import ConfigManager
from .proxy_core import create_app


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="mimo-proxy",
        description="MiMo Reasoning Content Proxy",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="CLI 模式（由 Tauri sidecar 或手动调用）",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="启用 DEBUG 级别日志",
    )
    args = parser.parse_args()

    _setup_logging(args.verbose)

    manager = ConfigManager()
    cfg = manager.get()
    if not cfg.endpoints:
        print("未配置任何 endpoint，请先在 ~/Library/Application Support/MiMoProxy/config.json 中添加。", file=sys.stderr)
        return 1

    app = create_app(cfg)
    print(f"MiMo Proxy v2.0 on {cfg.host}:{cfg.port}")
    print(f"  默认 endpoint: {cfg.default_name} → {cfg.default_endpoint().base_url if cfg.default_endpoint() else '-'}")
    for ep in cfg.endpoints:
        marker = "✓" if ep.name == cfg.default_name else " "
        state = "启用" if ep.enabled else "禁用"
        print(f"   [{marker}] /{ep.name}/v1/chat/completions  ({state})  →  {ep.base_url}")
    print()
    print(f"   Trae 配置地址: http://127.0.0.1:{cfg.port}/v1/chat/completions")
    print()

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())