"""滑动窗口限流器（接口骨架，待实现）。"""
from __future__ import annotations

import time


class SlidingWindowRateLimiter:
    """滑动窗口限流器。

    语义见任务说明：仅统计距今严格小于 window_seconds 的请求；
    allow 在超限时不消耗配额；不同 key 相互隔离。
    """

    def __init__(self, max_requests: int, window_seconds: float, clock=time.monotonic):
        raise NotImplementedError

    def allow(self, key: str) -> bool:
        raise NotImplementedError

    def remaining(self, key: str) -> int:
        raise NotImplementedError

    def reset(self, key: str) -> None:
        raise NotImplementedError
