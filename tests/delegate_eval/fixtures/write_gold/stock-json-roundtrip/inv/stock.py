"""Stock levels per SKU."""
import json


class Stock:
    def __init__(self):
        self._levels: dict[str, int] = {}

    def level(self, sku: str) -> int:
        return self._levels.get(sku, 0)

    def add(self, sku: str, qty: int) -> None:
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"quantity must be a positive integer, got {qty!r}")
        self._levels[sku] = self.level(sku) + qty

    def remove(self, sku: str, qty: int) -> None:
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"quantity must be a positive integer, got {qty!r}")
        self._levels[sku] = self.level(sku) - qty

    def to_json(self) -> str:
        return json.dumps({sku: n for sku, n in self._levels.items() if n != 0}, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Stock":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("stock JSON must be an object")
        stock = cls()
        for sku, level in data.items():
            if isinstance(level, bool) or not isinstance(level, int):
                raise ValueError(f"level for {sku!r} is not an int: {level!r}")
            stock._levels[sku] = level
        return stock
