from __future__ import annotations

import asyncio
import hmac
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"])
COOKIE_NAME = "gateway_dashboard_session"


class LoginRequest(BaseModel):
    password: str


async def require_operator(request: Request) -> None:
    if not await request.app.state.store.valid_session(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Dashboard session expired. Sign in again.")


@router.get("/", include_in_schema=False)
@router.get("/landing", include_in_schema=False)
async def landing():
    return FileResponse(Path(__file__).parent / "static" / "landing.html")


@router.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "dashboard.html")


@router.post("/admin/login")
async def login(body: LoginRequest, request: Request, response: Response):
    expected = request.app.state.settings.operator_password
    if not expected or not hmac.compare_digest(body.password, expected):
        raise HTTPException(status_code=401, detail="Invalid dashboard password")
    token = await request.app.state.store.create_session(request.app.state.settings.dashboard_session_hours)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        max_age=request.app.state.settings.dashboard_session_hours * 3600,
    )
    return {"ok": True, "expires_in_hours": request.app.state.settings.dashboard_session_hours}


@router.post("/admin/logout", dependencies=[Depends(require_operator)])
async def logout(request: Request, response: Response):
    await request.app.state.store.revoke_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


from app.logger import get_recent_logs


@router.get("/admin/logs", dependencies=[Depends(require_operator)])
async def get_logs(limit: int = 150):
    """Return recent in-memory log buffer for dashboard viewing."""
    return {"logs": get_recent_logs(limit=limit)}


@router.post("/admin/validate-sessions", dependencies=[Depends(require_operator)])
async def validate_sessions(request: Request):
    """Explicitly check authentication and chat input readiness across all providers."""
    browser_manager = request.app.state.browser
    logger.info("Manual validation of all browser sessions triggered from dashboard.")
    results = await browser_manager.check_all_providers()
    
    # Update SQLite store with latest validation results
    for name, data in results.items():
        st = "healthy" if data.get("authenticated") else "auth_failed"
        err = None if data.get("authenticated") else data.get("message")
        try:
            await request.app.state.store.update_provider(name, st, err)
        except Exception:
            pass

    return {"ok": True, "results": results}


class ProviderActionRequest(BaseModel):
    provider: str | None = None


@router.post("/admin/provider-login", dependencies=[Depends(require_operator)])
async def provider_login(body: ProviderActionRequest, request: Request):
    """Launch visible browser window for interactive account sign-in for a specific provider or all."""
    try:
        browser_manager = request.app.state.browser
        provider = body.provider.strip().lower() if body.provider else None
        url = await browser_manager.launch_interactive_login(provider)
        target_name = provider.upper() if provider else "ALL PROVIDERS"
        return {
            "ok": True,
            "provider": provider,
            "url": url,
            "message": f"Chrome opened for {target_name}. Please sign into your account, then click 'Save Session & Refresh'.",
        }
    except Exception as e:
        logger.error(f"Failed to launch login browser for {body.provider}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/save-session", dependencies=[Depends(require_operator)])
async def save_session(body: ProviderActionRequest, request: Request):
    """Save active browser session, close interactive window, and validate session status."""
    try:
        browser_manager = request.app.state.browser
        provider = body.provider.strip().lower() if body.provider else None
        res = await browser_manager.save_session_and_reload(provider)

        # Update SQLite store
        if provider and isinstance(res, dict) and "authenticated" in res:
            st = "healthy" if res.get("authenticated") else "auth_failed"
            err = None if res.get("authenticated") else res.get("message")
            await request.app.state.store.update_provider(provider, st, err)
        elif isinstance(res, dict):
            for name, data in res.items():
                if isinstance(data, dict):
                    st = "healthy" if data.get("authenticated") else "auth_failed"
                    err = None if data.get("authenticated") else data.get("message")
                    await request.app.state.store.update_provider(name, st, err)

        return {"ok": True, "result": res}
    except Exception as e:
        logger.error(f"Failed to save session for {body.provider}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/provider-refresh", dependencies=[Depends(require_operator)])
async def provider_refresh(body: ProviderActionRequest, request: Request):
    """Refresh and validate an individual provider session."""
    if not body.provider:
        raise HTTPException(status_code=400, detail="Missing 'provider' field")
    try:
        browser_manager = request.app.state.browser
        provider = body.provider.strip().lower()
        res = await browser_manager.refresh_provider(provider)
        st = "healthy" if res.get("authenticated") else "auth_failed"
        err = None if res.get("authenticated") else res.get("message")
        await request.app.state.store.update_provider(provider, st, err)
        return {"ok": True, "result": res}
    except Exception as e:
        logger.error(f"Failed to refresh provider {body.provider}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/launch-login", dependencies=[Depends(require_operator)])
async def launch_login(request: Request):
    """Launch visible browser window for interactive account sign-in (all providers)."""
    try:
        browser_manager = request.app.state.browser
        url = await browser_manager.launch_interactive_login(None)
        return {"ok": True, "message": "Chrome launched. Check your taskbar for the browser window to log in."}
    except Exception as e:
        logger.error(f"Failed to launch login browser: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/overview", dependencies=[Depends(require_operator)])
async def overview(request: Request):
    states = await request.app.state.store.provider_states()
    providers: dict[str, dict[str, object]] = {}

    # Check live browser status
    browser_manager = request.app.state.browser
    for name, prov in browser_manager.providers.items():
        stored_state = states.get(name, {})
        cached_auth = browser_manager._auth_cache.get(name)
        
        # Determine status: active/ready, needs_login, rate_limited
        if browser_manager._cooldown_until.get(name, 0) > time.time():
            computed_status = "rate_limited"
            last_err = "Temporary rate limit cooldown active"
        elif cached_auth is not None:
            is_auth, _, msg = cached_auth
            computed_status = "ready" if is_auth else "needs_login"
            last_err = None if is_auth else msg
        else:
            computed_status = stored_state.get("status", "unknown")
            last_err = stored_state.get("last_error")

        providers[name] = {
            "name": name,
            "provider": f"{name} (web free-tier)",
            "models": prov.model_aliases,
            "status": computed_status,
            "last_error": last_err,
            "credential_configured": True,
            "type": "browser",
        }

    # Also list LiteLLM API providers if enabled
    if hasattr(request.app.state.gateway, "model_list") and request.app.state.gateway.model_list:
        for item in request.app.state.gateway.model_list:
            params = item["litellm_params"]
            provider = params["model"].split("/", 1)[0]
            if provider not in providers:
                providers.setdefault(
                    provider,
                    {
                        "provider": provider,
                        "models": [],
                        "credential_configured": bool(params.get("api_key")),
                        "status": "configured" if bool(params.get("api_key")) else "missing_key",
                        "type": "api",
                    },
                )
            if item["model_name"] not in providers[provider]["models"]:
                providers[provider]["models"].append(item["model_name"])

    tunnel_url = getattr(request.app.state, "tunnel_url", None)

    return {
        "providers": list(providers.values()),
        "stats": await request.app.state.store.stats(),
        "tunnel_url": tunnel_url,
        "session_hours": request.app.state.settings.dashboard_session_hours,
        "master_key": request.app.state.settings.litellm_master_key or "",
    }
