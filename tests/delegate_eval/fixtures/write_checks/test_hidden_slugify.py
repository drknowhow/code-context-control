from inv.text import slugify


def test_basic():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  Multiple   spaces  ") == "multiple-spaces"
    assert slugify("already-a-slug") == "already-a-slug"
    assert slugify("C3 v2.139") == "c3-v2-139"


def test_non_ascii_letters_are_separators():
    assert slugify("Déjà vu") == "d-j-vu"


def test_empty_becomes_item():
    assert slugify("") == "item"
    assert slugify("!!!") == "item"


def test_truncation():
    assert slugify("hello world", max_len=5) == "hello"
    assert slugify("hello world", max_len=6) == "hello"
    assert slugify("a" * 59 + " b") == "a" * 59
    long = slugify("word " * 40)
    assert len(long) <= 60 and not long.endswith("-")
