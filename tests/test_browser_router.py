import pytest
from app.browser.providers.base import format_messages
from app.browser.manager import BrowserManager
from app.services import GatewayService


def test_format_messages_single_user():
    messages = [{"role": "user", "content": "Hello world"}]
    assert format_messages(messages) == "Hello world"


def test_format_messages_system_and_user():
    messages = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "What is Python?"}
    ]
    prompt = format_messages(messages)
    assert "[System Instructions]" in prompt
    assert "Be concise." in prompt
    assert "What is Python?" in prompt


def test_format_messages_multiturn():
    messages = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
        {"role": "user", "content": "Who made you?"}
    ]
    prompt = format_messages(messages)
    assert "User: Hi" in prompt
    assert "Assistant: Hello!" in prompt
    assert "User: Who made you?" in prompt


def test_browser_manager_model_resolution():
    manager = BrowserManager()
    
    # Test ChatGPT aliases
    assert manager.get_provider_for_model("chatgpt").name == "chatgpt"
    assert manager.get_provider_for_model("gpt-4o").name == "chatgpt"
    assert manager.get_provider_for_model("gpt-4o-mini").name == "chatgpt"

    # Test Claude aliases
    assert manager.get_provider_for_model("claude").name == "claude"
    assert manager.get_provider_for_model("claude-3-5-sonnet").name == "claude"

    # Test DeepSeek aliases
    assert manager.get_provider_for_model("deepseek").name == "deepseek"
    assert manager.get_provider_for_model("deepseek-chat").name == "deepseek"
    assert manager.get_provider_for_model("deepseek-r1").name == "deepseek"

    # Test Grok aliases
    assert manager.get_provider_for_model("grok").name == "grok"
    assert manager.get_provider_for_model("grok-3").name == "grok"

    # Test Gemini aliases
    assert manager.get_provider_for_model("gemini").name == "gemini"
    assert manager.get_provider_for_model("gemini-1.5-flash").name == "gemini"

    # Test Perplexity aliases
    assert manager.get_provider_for_model("perplexity").name == "perplexity"
    assert manager.get_provider_for_model("sonar").name == "perplexity"

    # Test Mistral aliases
    assert manager.get_provider_for_model("mistral").name == "mistral"
    assert manager.get_provider_for_model("lechat").name == "mistral"

    # Test HuggingFace aliases
    assert manager.get_provider_for_model("huggingface").name == "huggingface"
    assert manager.get_provider_for_model("hf-chat").name == "huggingface"

    # Test Poe aliases
    assert manager.get_provider_for_model("poe").name == "poe"
    assert manager.get_provider_for_model("poe-chat").name == "poe"

    # Test K2 Think aliases
    assert manager.get_provider_for_model("k2think").name == "k2think"
    assert manager.get_provider_for_model("k2").name == "k2think"

    # Test Ollama aliases
    assert manager.get_provider_for_model("ollama").name == "ollama"
    assert manager.get_provider_for_model("llama3").name == "ollama"

    # Test auto fallback
    assert manager.get_provider_for_model("auto") is not None


def test_gateway_service_browser_models():
    service = GatewayService()
    model_ids = [m["id"] for m in service.models]
    
    assert "chatgpt" in model_ids
    assert "claude" in model_ids
    assert "deepseek" in model_ids
    assert "grok" in model_ids
    assert "gemini" in model_ids
    assert "perplexity" in model_ids
    assert "mistral" in model_ids
    assert "huggingface" in model_ids
    assert "poe" in model_ids
    assert "k2think" in model_ids
    assert "ollama" in model_ids
    assert "auto" in model_ids

    assert service.is_browser_model("chatgpt") is True
    assert service.is_browser_model("gpt-4o") is True
    assert service.is_browser_model("claude-3-5-sonnet") is True
    assert service.is_browser_model("deepseek-r1") is True
    assert service.is_browser_model("grok") is True
    assert service.is_browser_model("gemini") is True
    assert service.is_browser_model("perplexity") is True
    assert service.is_browser_model("mistral") is True
    assert service.is_browser_model("huggingface") is True
    assert service.is_browser_model("poe") is True
    assert service.is_browser_model("k2think") is True
    assert service.is_browser_model("ollama") is True
    assert service.is_browser_model("auto") is True


@pytest.mark.asyncio
async def test_routing_skips_unauthenticated_providers():
    from app.browser.providers.base import AuthenticationRequiredError
    import time

    manager = BrowserManager()
    now = time.time()

    # Mark Claude as not logged in
    manager._auth_cache["claude"] = (False, now, "Sign in needed")

    # Requesting Claude directly should fail fast without running browser
    with pytest.raises(AuthenticationRequiredError) as exc_info:
        await manager.select_and_validate_provider("claude")
    assert "not logged in" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_routing_balances_load_to_least_loaded():
    import time
    manager = BrowserManager()
    now = time.time()

    # Pre-cache multiple providers as authenticated
    manager._auth_cache["chatgpt"] = (True, now, "Active")
    manager._auth_cache["deepseek"] = (True, now, "Active")
    manager._auth_cache["gemini"] = (True, now, "Active")

    # Put heavy in-flight load on chatgpt and deepseek
    manager._in_flight["chatgpt"] = 4
    manager._in_flight["deepseek"] = 2
    manager._in_flight["gemini"] = 0

    # Auto router should select the least-loaded authenticated provider (gemini)
    selected = await manager.select_and_validate_provider("auto")
    assert selected.name == "gemini"
