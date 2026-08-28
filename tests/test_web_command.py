import sys
import types

from typer.testing import CliRunner

from cli.main import app


def test_web_command_starts_uvicorn_without_api_key(monkeypatch):
    called = {}

    def fake_run(application, **kwargs):
        called.update(app=application, **kwargs)

    fake_web_app = types.ModuleType("web.app")
    fake_web_app.app = object()
    monkeypatch.setitem(sys.modules, "web.app", fake_web_app)
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = CliRunner().invoke(app, ["web", "--port", "8123"])
    assert result.exit_code == 0, result.output
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8123


def test_web_command_allows_external_host_binding(monkeypatch):
    called = {}

    def fake_run(application, **kwargs):
        called.update(app=application, **kwargs)

    fake_web_app = types.ModuleType("web.app")
    fake_web_app.app = object()
    monkeypatch.setitem(sys.modules, "web.app", fake_web_app)
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = CliRunner().invoke(app, ["web", "--host", "0.0.0.0", "--port", "8124"])
    assert result.exit_code == 0, result.output
    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8124
