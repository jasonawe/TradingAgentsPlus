"""Tests for RateLimiter (v3 spec §7.2 #5).

覆盖:
- 构造参数校验
- acquire 非阻塞 + 计数
- acquire_or_block 阻塞等待 + sleep 时间
- wait_seconds 不消耗 token
- token bucket refill 行为(随时间补充)
- thread-safe 并发 acquire
- reset()
- PROVIDER_RATE_LIMITS / get_rate_limiter / register_provider_rate_limit
"""
from __future__ import annotations

import threading
import time

import pytest


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_construction_rejects_invalid_args():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    with pytest.raises(ValueError):
        RateLimiter(rate=0, capacity=1)
    with pytest.raises(ValueError):
        RateLimiter(rate=-1, capacity=1)
    with pytest.raises(ValueError):
        RateLimiter(rate=1, capacity=0)
    with pytest.raises(ValueError):
        RateLimiter(rate=1, capacity=0.5)


def test_construction_full_bucket():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=2.0, capacity=5.0)
    assert rl.available == 5.0
    assert rl.stats()["capacity"] == 5.0


# ---------------------------------------------------------------------------
# acquire (non-blocking)
# ---------------------------------------------------------------------------
def test_acquire_drains_bucket_then_returns_false():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=2.0, capacity=3.0)
    assert rl.acquire() is True
    assert rl.acquire() is True
    assert rl.acquire() is True
    # 第 4 个会失败 — bucket 还没补充
    assert rl.acquire() is False
    assert rl.stats()["throttled"] == 1
    assert rl.stats()["acquired"] == 3


def test_acquire_refills_over_time():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=10.0, capacity=1.0)  # 1 token, 10/s refill
    assert rl.acquire() is True
    # 立刻再 acquire 应该 false
    assert rl.acquire() is False
    # 100ms 后应该有 1 个 token (10/s * 0.1 = 1)
    time.sleep(0.11)
    assert rl.acquire() is True


def test_acquire_multi_tokens():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=5.0, capacity=5.0)
    assert rl.acquire(tokens=3.0) is True
    assert rl.available == pytest.approx(2.0, abs=1e-3)
    assert rl.acquire(tokens=2.0) is True
    assert rl.acquire(tokens=1.0) is False  # bucket empty


def test_acquire_rejects_non_positive_tokens():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=1.0, capacity=1.0)
    with pytest.raises(ValueError):
        rl.acquire(tokens=0)
    with pytest.raises(ValueError):
        rl.acquire(tokens=-1)


# ---------------------------------------------------------------------------
# acquire_or_block
# ---------------------------------------------------------------------------
def test_acquire_or_block_immediate_when_available():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=5.0, capacity=5.0)
    slept = rl.acquire_or_block()
    assert slept == 0.0


def test_acquire_or_block_waits_for_refill():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=10.0, capacity=1.0)
    rl.acquire()  # drain bucket
    t0 = time.monotonic()
    slept = rl.acquire_or_block()
    elapsed = time.monotonic() - t0
    # 至少等了 ~0.1s 让 bucket 补回 1 token
    assert elapsed >= 0.05
    assert slept >= 0.05
    assert rl.available == pytest.approx(0.0, abs=1e-3)


def test_acquire_or_block_increments_throttled():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=20.0, capacity=1.0)
    rl.acquire()
    rl.acquire_or_block()
    # acquire_or_block 在等待期间内部 throttled += 1
    assert rl.stats()["throttled"] >= 1


# ---------------------------------------------------------------------------
# wait_seconds
# ---------------------------------------------------------------------------
def test_wait_seconds_zero_when_available():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=2.0, capacity=3.0)
    assert rl.wait_seconds() == 0.0


def test_wait_seconds_estimate():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=2.0, capacity=3.0)
    rl.acquire()  # 2 tokens left
    rl.acquire()  # 1 left
    rl.acquire()  # 0 left
    # 还要等 1/2 = 0.5s 才能拿下一个
    w = rl.wait_seconds()
    assert 0.4 < w < 0.6
    # wait_seconds 不消耗 token
    assert rl.available == pytest.approx(0.0, abs=1e-3)


def test_wait_seconds_does_not_change_throttled():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=2.0, capacity=1.0)
    rl.acquire()
    before = rl.stats()["throttled"]
    rl.wait_seconds()
    assert rl.stats()["throttled"] == before


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------
def test_reset_restores_full_capacity():
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=2.0, capacity=5.0)
    for _ in range(5):
        rl.acquire()
    assert rl.available == pytest.approx(0.0, abs=1e-3)
    rl.reset()
    assert rl.available == 5.0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_concurrent_acquires_consume_bes_consistently():
    """10 个 thread × 50 次 =  期望 (cap  20) →  成功 20, throttle 480。"""
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=1.0, capacity=20.0)
    results = []
    lock = threading.Lock()

    def worker():
        ok = 0
        for _ in range(50):
            if rl.acquire():
                ok += 1
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 10 threads × 50 = 500 acquires, cap = 20
    total_ok = sum(results)
    assert total_ok == 20
    assert rl.stats()["acquired"] == 20


# ---------------------------------------------------------------------------
# PROVIDER_RATE_LIMITS / registry helpers
# ---------------------------------------------------------------------------
def test_provider_rate_limits_has_known_providers():
    from tradingagents.agent_harness.core.rate_limiter import PROVIDER_RATE_LIMITS
    assert "alpha_vantage" in PROVIDER_RATE_LIMITS
    assert "eastmoney" in PROVIDER_RATE_LIMITS
    assert "yfinance" in PROVIDER_RATE_LIMITS
    assert "akshare" in PROVIDER_RATE_LIMITS


def test_alpha_vantage_rate_is_5_per_minute():
    from tradingagents.agent_harness.core.rate_limiter import PROVIDER_RATE_LIMITS
    bucket = PROVIDER_RATE_LIMITS["alpha_vantage"]
    assert bucket.rate == pytest.approx(5.0 / 60.0, abs=1e-6)
    assert bucket.capacity == 1.0


def test_get_rate_limiter_unknown_provider_gets_default():
    from tradingagents.agent_harness.core.rate_limiter import (
        RateLimiter, get_rate_limiter,
    )
    rl = get_rate_limiter("totally-unknown-provider-xyz")
    assert isinstance(rl, RateLimiter)
    # Default = 1 rps, capacity 1
    assert rl.rate == 1.0
    assert rl.capacity == 1.0


def test_register_provider_rate_limit_overrides():
    from tradingagents.agent_harness.core.rate_limiter import (
        PROVIDER_RATE_LIMITS, get_rate_limiter, register_provider_rate_limit,
    )
    register_provider_rate_limit("custom_provider", rate=7.0, capacity=14.0)
    rl = get_rate_limiter("custom_provider")
    assert rl.rate == 7.0
    assert rl.capacity == 14.0
    assert PROVIDER_RATE_LIMITS["custom_provider"].rate == 7.0
    # Cleanup
    PROVIDER_RATE_LIMITS.pop("custom_provider", None)


def test_register_provider_rate_limit_rejects_invalid():
    from tradingagents.agent_harness.core.rate_limiter import register_provider_rate_limit
    with pytest.raises(ValueError):
        register_provider_rate_limit("x", rate=0, capacity=1)
    with pytest.raises(ValueError):
        register_provider_rate_limit("y", rate=1, capacity=0.5)


def test_acquire_or_block_multi_token_consumes_correctly():
    """acquire_or_block(tokens=2) 必须等到 ≥ 2 个 token 才返。"""
    from tradingagents.agent_harness.core.rate_limiter import RateLimiter
    rl = RateLimiter(rate=20.0, capacity=5.0)
    # drain 到 1 个 (实际 1.0 + 极小 refill)
    for _ in range(4):
        rl.acquire()
    avail_before = rl.available
    assert avail_before == pytest.approx(1.0, abs=1e-2)
    t0 = time.monotonic()
    slept = rl.acquire_or_block(tokens=2.0)
    elapsed = time.monotonic() - t0
    # 需要再补 1 个 token,20 rps → ~0.05s
    assert elapsed >= 0.02
    # acquire_or_block 返回后,bucket 被耗到 ~0
    # (sleep 时长 = deficit/rate,刚好补够)。返回后几 ms(lock + Python
    # overhead)再 refill 会多补几个 token,rate=20 下 5ms=0.1 token。
    assert rl.available < 0.2
