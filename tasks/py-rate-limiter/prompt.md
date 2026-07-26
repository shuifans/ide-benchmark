<!-- prompt_version: v1 -->
# 任务：实现滑动窗口限流器

当前目录下有一个 Python 模块 `rate_limiter.py`，其中 `SlidingWindowRateLimiter` 类只有接口骨架，尚未实现。

请实现一个**滑动窗口**限流器，语义如下：

1. `SlidingWindowRateLimiter(max_requests, window_seconds, clock)`：
   - `max_requests`：窗口内允许的最大请求数（正整数）；
   - `window_seconds`：窗口长度（正数，秒）；
   - `clock`：返回当前时间（秒，float）的可调用对象，默认 `time.monotonic`。实现中**必须通过 `clock` 取时间**，不得直接调用 time 模块；
   - `max_requests <= 0` 或 `window_seconds <= 0` 时抛 `ValueError`。
2. `allow(key)`：判断标识为 `key` 的调用方此刻能否再发一次请求。
   - 设当前时间为 `now`，仅统计满足 `now - t < window_seconds` 的历史请求（即恰好距今 `window_seconds` 的请求已过期）；
   - 若窗口内请求数 `< max_requests`：记录本次请求并返回 `True`；
   - 否则返回 `False`，且**不**记录本次请求；
   - 不同 `key` 互不影响。
3. `remaining(key)`：返回该 `key` 此刻窗口内还可发的请求数（不消耗配额）。
4. `reset(key)`：清空该 `key` 的历史记录。

工程要求：

- 动手前先给出简要实现计划；
- 采用测试先行：先为上述语义编写你自己的测试（含边界与失败路径），再实现，最后确保测试全部通过；
- 不要修改 `rate_limiter.py` 中已给出的公开接口签名；
- 不要引入第三方依赖；测试中不要使用 `time.sleep`（用注入的 clock 控制时间）。
