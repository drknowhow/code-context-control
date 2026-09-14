"""Stock levels per SKU."""


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
