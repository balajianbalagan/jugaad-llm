import pytest
from unittest.mock import AsyncMock, MagicMock
from app.dashboard import provider_login, save_session, provider_refresh, ProviderActionRequest, landing, dashboard
from fastapi import HTTPException


@pytest.fixture
def mock_app_state():
    state = MagicMock()
    state.browser = MagicMock()
    state.browser.launch_interactive_login = AsyncMock(return_value="https://poe.com/login")
    state.browser.save_session_and_reload = AsyncMock(return_value={
        "provider": "poe",
        "authenticated": True,
        "message": "Ready",
        "status": "ready"
    })
    state.browser.refresh_provider = AsyncMock(return_value={
        "provider": "poe",
        "authenticated": True,
        "message": "Ready",
        "status": "ready"
    })
    state.store = MagicMock()
    state.store.update_provider = AsyncMock()
    return state


@pytest.mark.asyncio
async def test_provider_login(mock_app_state):
    req = MagicMock()
    req.app.state = mock_app_state

    body = ProviderActionRequest(provider="poe")
    res = await provider_login(body, req)

    assert res["ok"] is True
    assert res["provider"] == "poe"
    mock_app_state.browser.launch_interactive_login.assert_awaited_once_with("poe")


@pytest.mark.asyncio
async def test_save_session(mock_app_state):
    req = MagicMock()
    req.app.state = mock_app_state

    body = ProviderActionRequest(provider="poe")
    res = await save_session(body, req)

    assert res["ok"] is True
    mock_app_state.browser.save_session_and_reload.assert_awaited_once_with("poe")
    mock_app_state.store.update_provider.assert_awaited_once_with("poe", "healthy", None)


@pytest.mark.asyncio
async def test_provider_refresh(mock_app_state):
    req = MagicMock()
    req.app.state = mock_app_state

    body = ProviderActionRequest(provider="poe")
    res = await provider_refresh(body, req)

    assert res["ok"] is True
    mock_app_state.browser.refresh_provider.assert_awaited_once_with("poe")
    mock_app_state.store.update_provider.assert_awaited_once_with("poe", "healthy", None)


@pytest.mark.asyncio
async def test_provider_refresh_missing_name(mock_app_state):
    req = MagicMock()
    req.app.state = mock_app_state

    body = ProviderActionRequest(provider=None)
    with pytest.raises(HTTPException) as exc:
        await provider_refresh(body, req)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_landing_and_dashboard_pages():
    landing_res = await landing()
    assert landing_res.status_code == 200
    assert "landing.html" in str(landing_res.path)

    dashboard_res = await dashboard()
    assert dashboard_res.status_code == 200
    assert "dashboard.html" in str(dashboard_res.path)
