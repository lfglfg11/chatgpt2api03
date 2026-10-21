"""邮箱 API 专用代理池（**只**给 GPTMail2 等邮箱服务用）。

背景与实测依据（2026-10-21/22，欧洲服务器）：

- GPTMail2 的 429 是 ``{"error":"Too many inbox requests","code":"inbox_rate_limited",
  "retry_after":60}``，按**出口 IP** 计的额度桶：单 IP 持续请求会被长期限流，
  而换一个新的出口 IP 后建邮箱立刻恢复（实测：warp 单 IP 8/8 全 429；
  换一条德国住宅代理 5/5 全 200；同一条代理连续 12/12 全 200）。
- 因此注册链路的 OpenAI 请求继续走 ``register.json`` 顶层 proxy（warp），
  只有邮箱 API 走这个池，代理流量只花在建邮箱/读信这类小请求上。

配置（全部走环境变量，避免改动 register.json）：
- ``CHATGPT2API_MAIL_PROXY_POOL=0``            关闭代理池（默认开启，前提是文件存在）
- ``CHATGPT2API_MAIL_PROXY_POOL_FILE=<path>``  代理列表路径（默认 ``data/mail_proxy_pool.txt``）
- ``CHATGPT2API_MAIL_PROXY_COOLDOWN_SECONDS``  单个代理被 429 后的冷却秒数（默认 60，上限 300）

代理文件格式：每行一个 ``socks5://user:pass@host:port``（``#`` 开头为注释）。
``socks5://`` 会自动改写成 ``socks5h://``，让目标域名在代理侧解析。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from services.config import DATA_DIR

DEFAULT_POOL_FILENAME = "mail_proxy_pool.txt"
DEFAULT_COOLDOWN_SECONDS = 60.0
MAX_COOLDOWN_SECONDS = 300.0
DEFAULT_TRANSPORT_COOLDOWN_SECONDS = 120.0

ENV_ENABLED = "CHATGPT2API_MAIL_PROXY_POOL"
ENV_POOL_FILE = "CHATGPT2API_MAIL_PROXY_POOL_FILE"
ENV_COOLDOWN = "CHATGPT2API_MAIL_PROXY_COOLDOWN_SECONDS"
ENV_TRANSPORT_COOLDOWN = "CHATGPT2API_MAIL_PROXY_TRANSPORT_COOLDOWN_SECONDS"

_RETRY_AFTER_RE = re.compile(r"retry_after['\"]?\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_ALLOWED_SCHEMES = ("socks5://", "socks5h://", "http://", "https://")

_lock = threading.Lock()
_pool: list[str] = []
_pool_path: str = ""
_pool_mtime: float = 0.0
_index = 0
_cooldown: dict[str, float] = {}
_total_picks = 0
_total_cooldowns = 0


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def pool_path() -> Path:
    raw = str(os.environ.get(ENV_POOL_FILE) or "").strip()
    if not raw:
        return Path(DATA_DIR) / DEFAULT_POOL_FILENAME
    path = Path(raw)
    return path if path.is_absolute() else Path(DATA_DIR) / path


def _normalize(entry: str) -> str:
    value = entry.strip()
    if value.lower().startswith("socks5://"):
        return "socks5h://" + value[len("socks5://"):]
    return value


def load_pool(force: bool = False) -> list[str]:
    """读取代理池文件（按 mtime 缓存；文件不存在或为空时返回空列表）。"""
    global _pool, _pool_path, _pool_mtime
    path = pool_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        with _lock:
            _pool, _pool_path, _pool_mtime = [], str(path), 0.0
            _cooldown.clear()
        return []
    with _lock:
        if not force and _pool and str(path) == _pool_path and mtime == _pool_mtime:
            return list(_pool)
    entries: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            item = line.strip()
            if not item or item.startswith("#"):
                continue
            if not item.lower().startswith(_ALLOWED_SCHEMES):
                continue
            entries.append(_normalize(item))
    except OSError:
        entries = []
    with _lock:
        changed = entries != _pool
        _pool = entries
        _pool_path = str(path)
        _pool_mtime = mtime
        if changed:
            _cooldown.clear()
    return list(entries)


def enabled() -> bool:
    """代理池是否生效：环境变量允许且文件里有可用条目。"""
    if not _env_flag(ENV_ENABLED, True):
        return False
    return bool(load_pool())


def pick(exclude: set[str] | None = None) -> str:
    """按轮询取一个不在冷却中的代理；全部在冷却中则返回空串。"""
    global _index, _total_picks
    excluded = set(exclude or set())
    now = time.monotonic()
    with _lock:
        if not _pool:
            return ""
        total = len(_pool)
        for offset in range(total):
            candidate = _pool[(_index + offset) % total]
            if candidate in excluded:
                continue
            until = _cooldown.get(candidate, 0.0)
            if until and until > now:
                continue
            _index = (_index + offset + 1) % total
            _total_picks += 1
            return candidate
    return ""


def cool_down(proxy: str, seconds: float | None = None) -> float:
    """把某个代理置入冷却（默认取环境配置，上限 MAX_COOLDOWN_SECONDS）。"""
    global _total_cooldowns
    base = _env_float(ENV_COOLDOWN, DEFAULT_COOLDOWN_SECONDS)
    wait = base if seconds is None else float(seconds or base)
    wait = max(1.0, min(wait, MAX_COOLDOWN_SECONDS))
    with _lock:
        _cooldown[proxy] = time.monotonic() + wait
        _total_cooldowns += 1
    return wait


def transport_cooldown_seconds() -> float:
    """代理出口 TLS/连接失败时的冷却秒数（默认 120s，避免反复用到坏出口）。"""
    return _env_float(ENV_TRANSPORT_COOLDOWN, DEFAULT_TRANSPORT_COOLDOWN_SECONDS)


def retry_after_seconds(value: Any, default: float | None = None) -> float:
    """从错误体/错误文本里取 retry_after（取不到就用默认值）。"""
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(value)
    else:
        text = str(value or "")
    match = _RETRY_AFTER_RE.search(text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return _env_float(ENV_COOLDOWN, DEFAULT_COOLDOWN_SECONDS) if default is None else default


def stats() -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        cooling = sum(1 for until in _cooldown.values() if until > now)
        return {
            "enabled": bool(_pool) and _env_flag(ENV_ENABLED, True),
            "path": _pool_path or str(pool_path()),
            "pool_size": len(_pool),
            "cooling": cooling,
            "available": max(0, len(_pool) - cooling),
            "total_picks": _total_picks,
            "total_cooldowns": _total_cooldowns,
        }


def reset() -> None:
    global _index, _total_picks, _total_cooldowns
    with _lock:
        _cooldown.clear()
        _index = 0
        _total_picks = 0
        _total_cooldowns = 0
