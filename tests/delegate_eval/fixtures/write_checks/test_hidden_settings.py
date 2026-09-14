from inv.settings import bulk_enabled


def test_bulk_enabled(monkeypatch):
    monkeypatch.delenv("FEATURE_BULK", raising=False)
    assert bulk_enabled() is False
    for value in ("1", "true", "YES", " True "):
        monkeypatch.setenv("FEATURE_BULK", value)
        assert bulk_enabled() is True, value
    for value in ("0", "no", "", "on"):
        monkeypatch.setenv("FEATURE_BULK", value)
        assert bulk_enabled() is False, value
