from web.repositories import SettingsRepository
from web.storage import SQLiteStore


def test_settings_whitelist_and_sources(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = SettingsRepository(store)
    repo.set("quote_ttl_seconds", "30", source="sqlite")
    assert repo.get("quote_ttl_seconds") == {"value": "30", "source": "sqlite"}
    repo.set("OPENAI_API_KEY", "secret", source="sqlite")
    assert repo.get("OPENAI_API_KEY") is None
    store.close()


def test_settings_all_uses_one_connection(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = SettingsRepository(store)
    repo.set("quote_ttl_seconds", "30")
    repo.set("output_language", "Chinese")
    calls = {"count": 0}
    original = store.connection
    def counted():
        calls["count"] += 1
        return original()
    monkeypatch.setattr(store, "connection", counted)
    assert repo.all()["quote_ttl_seconds"]["value"] == "30"
    assert calls["count"] == 1
    store.close()
