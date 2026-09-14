import pytest

from inv.csv_import import load_lines
from inv.orders import Line


def _write(tmp_path, text):
    path = tmp_path / "lines.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_any_column_order_extra_columns_and_blank_lines(tmp_path):
    path = _write(tmp_path, "qty,sku,unit_cents,note\n2,A-1,250,x\n\n1,B-2,999,y\n")
    assert load_lines(path) == [Line("A-1", 250, 2), Line("B-2", 999, 1)]


def test_bad_value_names_the_file_line(tmp_path):
    path = _write(tmp_path, "sku,unit_cents,qty\nA,100,1\nB,abc,2\n")
    with pytest.raises(ValueError, match=r"\b3\b"):
        load_lines(path)
    path = _write(tmp_path, "sku,unit_cents,qty\n\n\nA,100,x\n")
    with pytest.raises(ValueError, match=r"\b4\b"):
        load_lines(path)


def test_accepts_a_str_path(tmp_path):
    path = _write(tmp_path, "sku,unit_cents,qty\nA,100,1\n")
    assert load_lines(str(path)) == [Line("A", 100, 1)]
