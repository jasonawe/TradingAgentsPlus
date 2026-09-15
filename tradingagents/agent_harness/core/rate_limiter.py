"""Token-bucket rate limiter (v3 spec §7.2 #5).

Provider / tool 调用前过 ``RateLimiter.acquire()`` 防止触发 provider
的 429 限流。设计要点:

- **Token bucket** 算法:capacity 个 token,以 rate (tokens/sec) 速度补充
- **Thread-safe** (单一进程内 sync):一个 provider 一个 limiter
- **非阻塞 + 阻塞两种 acquire 风格**
- **每 provider 推荐速率** 集中放在 ``PROVIDER_RATE_LIMITS`` 里,
  alpha_vantage / eastmoney 等可一行配置

这是 utility 类,不强制绑定 — 现有 provider 不调它也能跑,只是
失去防 429 的兜底。集成在调用点即可。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Bucket:
    """不可变 rate-limit 配置 (rate per sec + max bucket size)."""
    rate: float          # tokens / second
    capacity: float      # max tokens in bucket


class RateLimiter:
    """线程安全的 token-bucket 限流器。

    Parameters
    ----------
    rate
        长期平均速率 (tokens / second)。必须 > 0。
    capacity
        bucket 最大容量 (= 突发上限)。必须 ≥ 1。

    Example
    -------
    >>> rl = RateLimiter(rate=5.0, capacity=5.0)  # 5 req/s, 突发 5
    >>> rl.acquire()           # True (初始 5 个 token)
    True
    >>> rl.acquire()           # True (剩 4)
    True
    """

    def __init__(self, rate: float, capacity: float) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._lock = threading.Lock()
        # 初始满 bucket,允许一次容量大小的突发
        self._tokens: float = float(capacity)
        self._last_refill: float = time.monotonic()
        # Stats
        self.acquired = 0
        self.throttled = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def acquire(self, tokens: float = 1.0) -> bool:
        """非阻塞:有可用 token 就扣一个返 True,否则返 False。"""
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                self.acquired += 1
                return True
            self.throttled += 1
            return False

    def acquire_or_block(self, tokens: float = 1.0) -> float:
        """阻塞直到能扣 ``tokens`` 个 token,返实际 sleep 的秒数。

        返回 0.0 表示 bucket 充裕直接拿到。

        实现要点:每次 sleep 后立即 acquire lock + ``_refill_locked``,
        把 ``last_refill`` 推到当前时刻,这样后续外部 ``available`` /
        ``stats`` 调用不会把 sleep 期间补的 token 再算一遍。
        """
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        deadline = time.monotonic() + 30.0  # 30s hard cap
        slept = 0.0
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self.acquired += 1
                    return slept
                # Need to wait: deficit / rate seconds
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            # sleep outside lock so other threads can proceed
            time.sleep(wait)
            slept += wait
            self.throttled += 1
            if time.monotonic() > deadline:
                LOGGER.warning("RateLimiter hard cap hit (waited %.2fs)", slept)
                return slept
            # Drain elapsed time into the bucket BEFORE re-evaluating,
            # so the next _refill_locked sees a fresh last_refill.
            with self._lock:
                self._refill_locked()

    def wait_seconds(self, tokens: float = 1.0) -> float:
        """不消耗 token,只算还要等多久才能 acquire。"""
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                return 0.0
            deficit = tokens - self._tokens
            return deficit / self.rate

    def reset(self) -> None:
        """重置为满 bucket (debug / test 用)。"""
        with self._lock:
            self._tokens = self.capacity
            self._last_refill = time.monotonic()

    @property
    def available(self) -> float:
        """当前可用 token 数(不消耗)。"""
        with self._lock:
            self._refill_locked()
            return self._tokens

    def stats(self) -> dict[str, float]:
        with self._lock:
            self._refill_locked()
            return {
                "rate": self.rate,
                "capacity": self.capacity,
                "available": self._tokens,
                "acquired": self.acquired,
                "throttled": self.throttled,
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _refill_locked(self) -> None:
        """按时间流逝补充 token。Caller MUST hold ``self._lock``."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now


# ---------------------------------------------------------------------------
# Provider 推荐速率 (官方文档 / 经验值)
# ---------------------------------------------------------------------------
#: 推荐的 provider rate limit 配置 — 调用方可以 override。
PROVIDER_RATE_LIMITS: dict[str, _Bucket] = {
    # Alpha Vantage 免费层:5 req/min, 75 req/天 — 用 5/60 rps
    "alpha_vantage": _Bucket(rate=5.0 / 60.0, capacity=1.0),
    # Eastmoney 单 IP 限流较宽 — 10 req/s, 突发 20
    "eastmoney": _Bucket(rate=10.0, capacity=20.0),
    # yfinance 非官方,2 req/s 较稳
    "yfinance": _Bucket(rate=2.0, capacity=5.0),
    # akshare 没公开限流,设个温和 20 rps
    "akshare": _Bucket(rate=20.0, capacity=40.0),
}


def get_rate_limiter(provider_name: str) -> RateLimiter:
    """根据 provider name 拿到对应推荐 RateLimiter。

    Unknown provider 走一个保守的默认值 (1 rps, capacity 1)。
    """
    bucket = PROVIDER_RATE_LIMITS.get(provider_name)
    if bucket is None:
        bucket = _Bucket(rate=1.0, capacity=1.0)
    return RateLimiter(rate=bucket.rate, capacity=bucket.capacity)


def register_provider_rate_limit(
    provider_name: str, rate: float, capacity: float,
) -> None:
    """运行时注册 / 覆盖一个 provider 的速率配置。"""
    if rate <= 0 or capacity < 1:
        raise ValueError(f"invalid rate/capacity: {rate}, {capacity}")
    PROVIDER_RATE_LIMITS[provider_name] = _Bucket(rate=rate, capacity=capacity)
