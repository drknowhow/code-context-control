"""Load order lines from CSV."""
import csv

from inv.orders import Line


def load_lines(path) -> list[Line]:
    """Lines from a CSV with sku, unit_cents and qty columns in any order."""
    lines: list[Line] = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                lines.append(Line(row["sku"], int(row["unit_cents"]), int(row["qty"])))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"line {reader.line_num}: {exc}") from None
    return lines
