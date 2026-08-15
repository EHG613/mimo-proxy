"""MiMo Proxy 客户端入口。

Tauri 2 桌面应用通过 ``python -m client --cli`` 启动代理 sidecar。
也可以直接 ``python -m client`` 启动纯命令行代理。
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import uvicorn

from .config import CONFIG_PATH, ConfigManager
from .proxy_core import create_app


def _watch_parent() -> None:
    """sidecar 模式下监控父进程（Tauri 应用）。

    dev 热重载或应用被强杀时，父进程不会有机会调用 kill 清理子进程，
    导致 Python 进程孤儿化并持续占用端口。这里通过轮询 ppid 检测
    父进程退出（macOS 上孤儿会被重挂到 PID 1），主动退出。
    """
    parent = os.getppid()

    def _watch() -> None:
        while True:
            time.sleep(1.0)
            if os.getppid() != parent:
                logging.getLogger("client.watchdog").info(
                    "检测到父进程退出，sidecar 自动停止"
                )
                os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()


def _watch_stdin_eof() -> None:
    """stdin 为管道（由 Tauri 父进程创建）时，监听 EOF 实现零延迟清理。

    父进程无论以何种方式终止（含被 SIGKILL 强杀），内核都会关闭管道写端，
    这里立即收到 EOF 并退出，无轮询延迟。与 ppid watchdog 互补：
    - stdin EOF：事件驱动，无延迟，依赖父进程用 piped stdin 启动本进程
    - ppid watchdog：轮询兜底，覆盖 stdin 不是管道的启动方式

    仅在 stdin 确实是管道时启用，避免手动在终端运行（tty）时误吞输入。
    """
    try:
        if not stat.S_ISFIFO(os.fstat(sys.stdin.fileno()).st_mode):
            return
    except OSError:
        return

    def _watch() -> None:
        try:
            while sys.stdin.buffer.read(1):
                pass
        except Exception:
            pass
        logging.getLogger("client.watchdog").info(
            "检测到父进程 stdin 管道关闭，sidecar 自动停止"
        )
        os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    # 文件日志：GUI 模式下 stdout 不可见，重试/降级行为需要可事后取证
    try:
        log_dir = CONFIG_PATH.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "proxy.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception as e:  # 日志失败不阻塞启动
        print(f"文件日志初始化失败（忽略）: {e}", file=sys.stderr)


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

    if args.cli:
        _watch_stdin_eof()
        _watch_parent()

    _setup_logging(args.verbose)

    manager = ConfigManager()
    cfg = manager.get()
    if not cfg.endpoints:
        print("未配置任何 endpoint，请先在 ~/.mimo-proxy/config.json 中添加。", file=sys.stderr)
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