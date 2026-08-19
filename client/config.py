"""配置管理：读写 ~/.mimo-proxy/config.json。

配置结构示例::

    {
      "host": "127.0.0.1",
      "port": 8899,
      "auto_start": true,
      "cache_max_size": 2000,
      "cache_ttl": 7200,
      "default_name": "default",
      "endpoints": [
        {
          "name": "default",
          "base_url": "https://one-api-test.liangyihui.net:8080/v1",
          "enabled": true,
          "vendor": "lyh"
        },
        {
          "name": "prod",
          "base_url": "https://api.xiaomimimo.com/v1",
          "enabled": false,
          "vendor": ""
        }
      ]
    }
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable


def _config_dir() -> Path:
    # 优先环境变量（Tauri 应用传入 / 用于测试 / 自定义部署）
    env_override = os.environ.get("MIMO_PROXY_CONFIG_DIR")
    if env_override:
        path = Path(env_override).expanduser()
    else:
        home = os.path.expanduser("~")
        path = Path(home) / ".mimo-proxy"
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_PATH = _config_dir() / "config.json"

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_name(name: str) -> str | None:
    """返回 None 表示合法，否则返回错误说明。"""
    name = (name or "").strip()
    if not name:
        return "名称不能为空"
    if not _NAME_RE.match(name):
        return "名称只能包含字母、数字、下划线和短横线"
    if name in {"v1", "models", "chat", "health"}:
        return f"名称 '{name}' 是保留字"
    return None


@dataclass
class Endpoint:
    name: str
    base_url: str
    enabled: bool = True
    vendor: str = ""

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.vendor = self.vendor.strip("/")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8899
    auto_start: bool = True
    cache_max_size: int = 2000
    cache_ttl: int = 7200
    default_name: str = "default"
    endpoints: list[Endpoint] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        endpoints = [
            Endpoint(
                name=e.get("name", ""),
                base_url=e.get("base_url", ""),
                enabled=bool(e.get("enabled", True)),
                vendor=e.get("vendor", ""),
            )
            for e in d.get("endpoints", [])
        ]
        return cls(
            host=d.get("host", "127.0.0.1"),
            port=int(d.get("port", 8899)),
            auto_start=bool(d.get("auto_start", True)),
            cache_max_size=int(d.get("cache_max_size", 2000)),
            cache_ttl=int(d.get("cache_ttl", 7200)),
            default_name=d.get("default_name", "default"),
            endpoints=endpoints,
        )

    def default_endpoint(self) -> Endpoint | None:
        for e in self.endpoints:
            if e.enabled and e.name == self.default_name:
                return e
        for e in self.endpoints:
            if e.enabled:
                return e
        return None

    def find_endpoint(self, name: str) -> Endpoint | None:
        for e in self.endpoints:
            if e.name == name:
                return e
        return None


_DEFAULT_CONFIG = Config(
    endpoints=[
        Endpoint(
            name="default",
            base_url="https://one-api-test.liangyihui.net:8080/v1",
            enabled=True,
        ),
    ],
)


class ConfigManager:
    """线程安全的配置管理器，支持监听变更。"""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._config: Config = self._load_unlocked()
        self._listeners: list[Callable[[Config], None]] = []

    @property
    def path(self) -> Path:
        return self._path

    def _load_unlocked(self) -> Config:
        if not self._path.exists():
            cfg = _DEFAULT_CONFIG
            self._save_unlocked(cfg)
            return cfg
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return Config.from_dict(data)
        except Exception:
            cfg = _DEFAULT_CONFIG
            try:
                self._save_unlocked(cfg)
            except Exception:
                pass
            return cfg

    def _save_unlocked(self, cfg: Config) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def load(self) -> Config:
        with self._lock:
            self._config = self._load_unlocked()
            self._notify()
            return self._config

    def save(self, cfg: Config) -> None:
        with self._lock:
            self._config = cfg
            self._save_unlocked(cfg)
            self._notify()

    def get(self) -> Config:
        with self._lock:
            return self._config

    def update(self, fn: Callable[[Config], None]) -> Config:
        with self._lock:
            fn(self._config)
            self._save_unlocked(self._config)
            cfg = self._config
        self._notify()
        return cfg

    def add_listener(self, fn: Callable[[Config], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def _notify(self) -> None:
        cfg = self._config
        for fn in list(self._listeners):
            try:
                fn(cfg)
            except Exception:
                pass
