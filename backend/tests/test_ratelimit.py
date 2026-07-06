"""test_ratelimit.py — Unit tests for the sliding-window rate limiter."""

import time

from ratelimit import SlidingWindowLimiter


class TestSlidingWindowLimiter:
    def test_allows_up_to_limit_then_blocks(self):
        lim = SlidingWindowLimiter(max_calls=3, window_seconds=60)
        assert [lim.hit("ip") for _ in range(4)] == [True, True, True, False]

    def test_keys_have_independent_budgets(self):
        lim = SlidingWindowLimiter(max_calls=1, window_seconds=60)
        assert lim.hit("a") is True
        assert lim.hit("b") is True   # different client, own budget
        assert lim.hit("a") is False  # first client exhausted

    def test_window_expiry_restores_budget(self):
        lim = SlidingWindowLimiter(max_calls=1, window_seconds=0.2)
        assert lim.hit("a") is True
        assert lim.hit("a") is False
        time.sleep(0.25)
        assert lim.hit("a") is True   # old hit slid out of the window
