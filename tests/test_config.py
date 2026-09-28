from app.config import _resolve_env


def test_resolves_environment_reference(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "value")
    assert _resolve_env("os.environ/DEMO_KEY") == "value"
    assert _resolve_env({"key": "os.environ/DEMO_KEY"}) == {"key": "value"}
