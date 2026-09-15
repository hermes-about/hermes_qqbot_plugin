import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable


class SlidingWindowRateLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._entries: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: tuple[str, str]) -> bool:
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            items = self._entries[key]
            while items and items[0] <= cutoff:
                items.popleft()
            if len(items) >= self._limit:
                return False
            items.append(now)
            return True
