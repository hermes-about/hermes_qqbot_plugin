import threading
import time
from collections.abc import Callable

from .models import PermissionSnapshot


class PermissionCache:
    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._items: dict[tuple[str, str], tuple[float, PermissionSnapshot]] = {}

    def get_or_load(
        self, platform: str, openid: str, loader: Callable[[], PermissionSnapshot]
    ) -> PermissionSnapshot:
        key = (platform, openid)
        now = self._clock()
        with self._lock:
            cached = self._items.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
        value = loader()
        if self._ttl > 0:
            with self._lock:
                self._items[key] = (self._clock() + self._ttl, value)
        return value

    def invalidate(self, platform: str, openid: str) -> None:
        with self._lock:
            self._items.pop((platform, openid), None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
