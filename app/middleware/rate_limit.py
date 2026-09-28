from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int = 60):
        self.limit, self.window_seconds = limit, window_seconds
        self.requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        bucket = self.requests[key]
        while bucket and bucket[0] <= now - self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded; retry in one minute")
        bucket.append(now)
