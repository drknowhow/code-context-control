import pytest

from inv.csv_import import load_lines
from inv.orders import Line


def test_reads_rows(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("sku,unit_cents,qty\nA,100,2\n", encoding="utf-8")
    assert load_lines(path) == [Line("A", 100, 2)]


def test_skips_blank_lines(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("qty,sku,unit_cents\n\n3,B,50\n", encoding="utf-8")
    assert load_lines(path) == [Line("B", 50, 3)]


def test_bad_qty_names_the_line(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("sku,unit_cents,qty\nA,100,x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="2"):
        load_lines(path)
