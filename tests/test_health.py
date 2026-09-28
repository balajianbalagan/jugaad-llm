import asyncio

from app.routes.health import health


class Gateway:
    models = [{"id": "one", "owned_by": "openai"}, {"id": "two", "owned_by": "groq"}]


class App:
    class State:
        gateway = Gateway()
    state = State()


class Request:
    app = App()


def test_health():
    result = asyncio.run(health(Request()))
    assert result["status"] == "ok"
    assert result["configured_models"] == 2
