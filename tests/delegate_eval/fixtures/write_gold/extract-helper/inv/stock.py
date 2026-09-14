"""Stock levels per SKU."""


def _validate_qty(qty) -> None:
    if not isinstance(qty, int) or qty <= 0:
        raise ValueError(f"quantity must be a positive integer, got {qty!r}")


class Stock:
    def __init__(self):
        self._levels: dict[str, int] = {}

    def level(self, sku: str) -> int:
        return self._levels.get(sku, 0)

    def add(self, sku: str, qty: int) -> None:
        _validate_qty(qty)
        self._levels[sku] = self.level(sku) + qty

    def remove(self, sku: str, qty: int) -> None:
        _validate_qty(qty)
        self._levels[sku] = self.level(sku) - qty
