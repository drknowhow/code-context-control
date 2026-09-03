"""Small LRU cache for invoice lookups."""
from collections import OrderedDict


class LruCache:
    def __init__(self, capacity: int = 256):
        self.capacity = capacity
        self._data: OrderedDict = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        if key in self._data:
            self._data.move_to_end(key)
            self.hits += 1
            return self._data[key]
        self.misses += 1
        return None

    def put(self, key, value) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        self.evict()

    def evict(self) -> int:
        """Drop least-recently-used entries until under capacity."""
        removed = 0
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)
            removed += 1
        return removed
