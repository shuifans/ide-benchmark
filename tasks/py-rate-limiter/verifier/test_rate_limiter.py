"""滑动窗口限流器 verifier：窗口边界 / 突发 / 按 key 隔离 / 配额语义 / 参数校验。"""
import pytest

from rate_limiter import SlidingWindowRateLimiter


class FakeClock:
    def __init__(self, now=0.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


@pytest.fixture
def clock():
    return FakeClock()


def make(max_requests=3, window=10.0, clock=None):
    return SlidingWindowRateLimiter(max_requests, window, clock or FakeClock())


def test_allows_up_to_max_within_window(clock):
    rl = SlidingWindowRateLimiter(3, 10.0, clock)
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is True


def test_blocks_over_max(clock):
    rl = SlidingWindowRateLimiter(2, 10.0, clock)
    assert rl.allow("k")
    assert rl.allow("k")
    assert rl.allow("k") is False


def test_rejected_request_does_not_consume_quota(clock):
    rl = SlidingWindowRateLimiter(1, 10.0, clock)
    assert rl.allow("k")
    # 连续拒绝多次后，窗口滑过仍应恢复 1 个配额（拒绝不占坑）
    for _ in range(5):
        assert rl.allow("k") is False
    clock.advance(10.0)
    assert rl.allow("k") is True


def test_window_boundary_exactly_window_expires(clock):
    rl = SlidingWindowRateLimiter(1, 10.0, clock)
    assert rl.allow("k")
    clock.advance(10.0)  # now - t == window → 已过期
    assert rl.allow("k") is True


def test_window_boundary_just_inside_still_counts(clock):
    rl = SlidingWindowRateLimiter(1, 10.0, clock)
    assert rl.allow("k")
    clock.advance(9.999)  # 仍在窗口内
    assert rl.allow("k") is False


def test_sliding_partial_expiry(clock):
    rl = SlidingWindowRateLimiter(2, 10.0, clock)
    assert rl.allow("k")          # t=0
    clock.advance(6.0)
    assert rl.allow("k")          # t=6
    assert rl.allow("k") is False
    clock.advance(4.0)            # t=10：t=0 的过期，t=6 的仍在
    assert rl.allow("k") is True
    assert rl.allow("k") is False


def test_per_key_isolation(clock):
    rl = SlidingWindowRateLimiter(1, 10.0, clock)
    assert rl.allow("a")
    assert rl.allow("b") is True   # b 不受 a 影响
    assert rl.allow("a") is False


def test_remaining_reports_without_consuming(clock):
    rl = SlidingWindowRateLimiter(3, 10.0, clock)
    assert rl.remaining("k") == 3
    rl.allow("k")
    assert rl.remaining("k") == 2
    assert rl.remaining("k") == 2  # 查询不消耗
    rl.allow("k")
    rl.allow("k")
    assert rl.remaining("k") == 0


def test_reset_clears_history(clock):
    rl = SlidingWindowRateLimiter(1, 10.0, clock)
    rl.allow("k")
    assert rl.allow("k") is False
    rl.reset("k")
    assert rl.allow("k") is True


def test_invalid_args_raise_value_error(clock):
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(0, 10.0, clock)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(-1, 10.0, clock)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(3, 0, clock)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(3, -5.0, clock)


def test_uses_injected_clock_not_wall_time():
    # 只推进注入时钟即可让配额恢复：证明实现依赖 clock 而非真实时间
    clock = FakeClock(1000.0)
    rl = SlidingWindowRateLimiter(1, 3600.0, clock)
    assert rl.allow("k")
    assert rl.allow("k") is False
    clock.advance(3600.0)
    assert rl.allow("k") is True
