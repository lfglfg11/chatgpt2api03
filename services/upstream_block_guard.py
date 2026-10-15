"""上游会话接口整体故障的进程内熔断器。

线上取证（2026-10-15 起，见 ``image_failure.is_upstream_model_blocked``）：
``POST /backend-api/conversation`` 与 ``/backend-api/f/conversation`` 对绝大多数
账号返回 ``404 {"detail":"Model not found"}``，而同一账号的
``/backend-api/me``、``/backend-api/models``、``/backend-api/f/conversation/prepare``
全部 200。换 Accept-Language / OAI-Language / timezone / 出口 IP / client version /
设备指纹 / model slug 都不改变结果，可用账号对任意 model 名都返回 200。

结论：这是上游整体故障（伪装成 404 的通用拒绝），不是账号问题。继续按账号故障
处理会同时造成三种浪费：

1. 每个请求白烧 ``image_max_account_attempts``（默认 4）个账号；
2. 把号池的 fail 计数与远程核验全部打满；
3. 24 小时制造 6000+ 次无效上游请求。

因此这里做进程内熔断：连续 N 次命中该故障就短路一段时间，期间直接快速失败；
冷却结束只放行一个探针请求，探针成功即恢复，失败则翻倍冷却（有上限）。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 180.0
DEFAULT_MAX_COOLDOWN_SECONDS = 1800.0

ENV_FAILURE_THRESHOLD = "CHATGPT2API_UPSTREAM_BLOCK_FAILURE_THRESHOLD"
ENV_COOLDOWN_SECONDS = "CHATGPT2API_UPSTREAM_BLOCK_COOLDOWN_SECONDS"
ENV_MAX_COOLDOWN_SECONDS = "CHATGPT2API_UPSTREAM_BLOCK_MAX_COOLDOWN_SECONDS"


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    return value if value > 0 else default


class UpstreamBlockGuard:
    """连续命中上游整体故障时短路请求，冷却后放行单个探针。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive = 0
        self._opened_at = 0.0
        self._cooldown = 0.0
        self._probe_in_flight = False
        self._total_blocked = 0
        self._last_blocked_at = 0.0
        self._last_recovered_at = 0.0

    @property
    def threshold(self) -> int:
        return _env_int(ENV_FAILURE_THRESHOLD, DEFAULT_FAILURE_THRESHOLD)

    @property
    def base_cooldown(self) -> float:
        return _env_float(ENV_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS)

    @property
    def max_cooldown(self) -> float:
        return _env_float(ENV_MAX_COOLDOWN_SECONDS, DEFAULT_MAX_COOLDOWN_SECONDS)

    def record_blocked(self) -> None:
        """记录一次真实命中的上游整体故障。"""
        now = time.monotonic()
        with self._lock:
            self._total_blocked += 1
            self._last_blocked_at = time.time()
            self._consecutive += 1
            if self._probe_in_flight:
                # 探针也失败了：重新开闸并翻倍冷却。
                self._probe_in_flight = False
                self._cooldown = min(
                    max(self._cooldown * 2, self.base_cooldown),
                    self.max_cooldown,
                )
                self._opened_at = now
                return
            if self._consecutive >= self.threshold:
                if self._opened_at <= 0.0:
                    self._cooldown = self.base_cooldown
                self._opened_at = now

    def record_success(self) -> None:
        """任意一次真实成功都说明上游恢复（或本进程判断有误），立即复位。"""
        with self._lock:
            recovered = self._opened_at > 0.0
            self._consecutive = 0
            self._opened_at = 0.0
            self._cooldown = 0.0
            self._probe_in_flight = False
            if recovered:
                self._last_recovered_at = time.time()

    def should_short_circuit(self) -> bool:
        """True 表示当前请求应立刻失败；冷却结束时放行一个探针并返回 False。"""
        now = time.monotonic()
        with self._lock:
            if self._opened_at <= 0.0:
                return False
            if now - self._opened_at < self._cooldown:
                return True
            if self._probe_in_flight:
                return True
            self._probe_in_flight = True
            return False

    def state(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            open_now = self._opened_at > 0.0
            remaining = (
                max(0.0, self._cooldown - (now - self._opened_at)) if open_now else 0.0
            )
            return {
                "open": open_now,
                "consecutive": self._consecutive,
                "threshold": self.threshold,
                "cooldown_seconds": round(self._cooldown, 3),
                "remaining_seconds": round(remaining, 3),
                "probe_in_flight": self._probe_in_flight,
                "total_blocked": self._total_blocked,
                "last_blocked_at": self._last_blocked_at,
                "last_recovered_at": self._last_recovered_at,
            }

    def reset(self) -> None:
        """测试与运维用：强制复位。"""
        with self._lock:
            self._consecutive = 0
            self._opened_at = 0.0
            self._cooldown = 0.0
            self._probe_in_flight = False


upstream_block_guard = UpstreamBlockGuard()
