from __future__ import annotations

import re
from fastapi import HTTPException, Request

COOKIE_NAME = "gateway_dashboard_session"


async def require_gateway_key(request: Request) -> None:
    settings = request.app.state.settings
    expected = settings.litellm_master_key
    if not expected:
        return

    # 1. Allow authenticated operator dashboard sessions
    session_cookie = request.cookies.get(COOKIE_NAME)
    if session_cookie and hasattr(request.app.state, "store"):
        if await request.app.state.store.valid_session(session_cookie):
            return

    # 2. Extract Authorization header (case-insensitive "Bearer <token>" or raw token)
    auth_header = request.headers.get("authorization", "").strip()
    supplied = None
    if auth_header:
        match = re.match(r"^Bearer\s+(.+)$", auth_header, flags=re.IGNORECASE)
        supplied = match.group(1).strip() if match else auth_header

    # 3. Fallbacks for other header or query parameter conventions
    if not supplied:
        supplied = (
            request.headers.get("x-api-key", "").strip()
            or request.headers.get("api-key", "").strip()
            or request.query_params.get("api_key", "").strip()
            or request.query_params.get("key", "").strip()
        )

    if supplied != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing gateway API key")
