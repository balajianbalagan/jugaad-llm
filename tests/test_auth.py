import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException
from app.middleware.auth import require_gateway_key, COOKIE_NAME


class MockSettings:
    def __init__(self, key="sk-test-master-key"):
        self.litellm_master_key = key


class MockStore:
    def __init__(self, valid_tokens=None):
        self.valid_tokens = set(valid_tokens or [])

    async def valid_session(self, token):
        return token in self.valid_tokens


def make_request(headers=None, cookies=None, query_params=None, master_key="sk-test-master-key", valid_tokens=None):
    request = MagicMock()
    request.app.state.settings = MockSettings(master_key)
    request.app.state.store = MockStore(valid_tokens)
    
    headers_dict = {k.lower(): v for k, v in (headers or {}).items()}
    request.headers = headers_dict
    request.cookies = cookies or {}
    request.query_params = query_params or {}
    return request


@pytest.mark.asyncio
async def test_require_gateway_key_disabled_when_none():
    req = make_request(master_key=None)
    # Should not raise
    await require_gateway_key(req)


@pytest.mark.asyncio
async def test_require_gateway_key_standard_bearer():
    req = make_request(headers={"Authorization": "Bearer sk-test-master-key"})
    await require_gateway_key(req)


@pytest.mark.asyncio
async def test_require_gateway_key_case_insensitive_bearer():
    req = make_request(headers={"Authorization": "bearer sk-test-master-key"})
    await require_gateway_key(req)


@pytest.mark.asyncio
async def test_require_gateway_key_raw_token():
    req = make_request(headers={"Authorization": "sk-test-master-key"})
    await require_gateway_key(req)


@pytest.mark.asyncio
async def test_require_gateway_key_x_api_key():
    req = make_request(headers={"X-API-Key": "sk-test-master-key"})
    await require_gateway_key(req)


@pytest.mark.asyncio
async def test_require_gateway_key_dashboard_session_allowed():
    req = make_request(
        cookies={COOKIE_NAME: "valid-session-token"},
        valid_tokens=["valid-session-token"],
    )
    # No Authorization header, but valid dashboard session
    await require_gateway_key(req)


@pytest.mark.asyncio
async def test_require_gateway_key_missing_fails():
    req = make_request()
    with pytest.raises(HTTPException) as exc_info:
        await require_gateway_key(req)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or missing gateway API key"


@pytest.mark.asyncio
async def test_require_gateway_key_invalid_token_fails():
    req = make_request(headers={"Authorization": "Bearer wrong-key"})
    with pytest.raises(HTTPException) as exc_info:
        await require_gateway_key(req)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or missing gateway API key"
