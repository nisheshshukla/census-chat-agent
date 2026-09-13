"""Small in-process LRU caches with TTL. The dataset is static, so cache hits are exact."""

from __future__ import annotations

import re
import time
from collections import OrderedDict


class TTLCache[V]:
    def __init__(self, max_items: int = 512, ttl_s: float = 3600.0) -> None:
        self.max_items = max_items
        self.ttl_s = ttl_s
        self._d: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> V | None:
        item = self._d.get(key)
        if item is None:
            self.misses += 1
            return None
        at, value = item
        if time.monotonic() - at > self.ttl_s:
            del self._d[key]
            self.misses += 1
            return None
        self._d.move_to_end(key)
        self.hits += 1
        return value

    def set(self, key: str, value: V) -> None:
        self._d[key] = (time.monotonic(), value)
        self._d.move_to_end(key)
        while len(self._d) > self.max_items:
            self._d.popitem(last=False)

    def __len__(self) -> int:
        return len(self._d)


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).rstrip(";").strip().lower()
