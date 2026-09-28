from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.browser.providers.base import (
    AuthenticationRequiredError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from app.middleware.auth import require_gateway_key
from app.models.request import ChatCompletionRequest

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict(exclude_none=True)
    return dict(value)


async def _sse(result: Any, model_name: str = "", store: Any = None, payload: dict | None = None, started_at: float = 0.0):
    first_token = False
    tokens = 0
    try:
        async for chunk in result:
            if not first_token:
                logger.info(f"[Chat Stream] First token delivered for model='{model_name}'")
                first_token = True
            tokens += 1
            yield f"data: {json.dumps(_as_dict(chunk), default=str)}\n\n"
        
        latency = int((time.perf_counter() - started_at) * 1000) if started_at else 0
        logger.info(f"[Chat Stream] Stream finished cleanly for model='{model_name}' ({tokens} chunks, {latency}ms)")
        if store:
            try:
                await store.log(
                    model=model_name,
                    provider_model=model_name,
                    status_code=200,
                    latency_ms=latency,
                    prompt_tokens=0,
                    completion_tokens=tokens,
                    total_tokens=tokens,
                    estimated_cost=0,
                    fallback_used=0,
                    error=None,
                )
            except Exception:
                pass
        yield "data: [DONE]\n\n"
    except Exception as exc:
        latency = int((time.perf_counter() - started_at) * 1000) if started_at else 0
        err_msg = str(exc)
        logger.error(f"[Chat Stream] Error during stream generation for '{model_name}': {err_msg}", exc_info=True)
        if store:
            try:
                await store.log(
                    model=model_name,
                    provider_model=model_name,
                    status_code=500,
                    latency_ms=latency,
                    prompt_tokens=0,
                    completion_tokens=tokens,
                    total_tokens=tokens,
                    estimated_cost=0,
                    fallback_used=0,
                    error=err_msg[:1000],
                )
            except Exception:
                pass
        err_payload = {
            "error": {
                "message": err_msg,
                "type": exc.__class__.__name__,
                "model": model_name,
            }
        }
        yield f"data: {json.dumps(err_payload)}\n\n"
        yield "data: [DONE]\n\n"


def _extract_provider_name(request: Request, model_name: str) -> str:
    key = model_name.lower()
    browser_manager = getattr(request.app.state, "browser", None)
    if browser_manager:
        for prov in browser_manager.providers:
            if prov in key:
                return prov
    for prov in ("chatgpt", "claude", "deepseek", "grok", "gemini", "perplexity", "mistral", "huggingface", "poe", "k2think", "ollama"):
        if prov in key:
            return prov
    configured = next((item for item in getattr(request.app.state.gateway, "model_list", []) if item["model_name"] == model_name), None)
    if configured:
        return configured["litellm_params"]["model"].split("/", 1)[0]
    return "browser"


@router.post("/v1/chat/completions", dependencies=[Depends(require_gateway_key)])
async def chat_completion(body: ChatCompletionRequest, request: Request):
    identity = request.headers.get("authorization") or (request.client.host if request.client else "anonymous")
    request.app.state.global_limiter.check("global")
    request.app.state.user_limiter.check(identity)
    payload = body.model_dump(exclude_none=True)
    started = time.perf_counter()
    provider_name = _extract_provider_name(request, body.model)

    last_msg = payload.get("messages", [{}])[-1].get("content", "") if payload.get("messages") else ""
    preview = repr(last_msg)[:60]
    logger.info(f"Incoming chat request: model='{body.model}', stream={body.stream}, prompt_preview={preview}")

    try:
        result = await request.app.state.gateway.complete(payload)
        if body.stream:
            return StreamingResponse(
                _sse(result, model_name=body.model, store=request.app.state.store, payload=payload, started_at=started),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

        response = _as_dict(result)
        usage = response.get("usage", {})
        latency = int((time.perf_counter() - started) * 1000)

        await request.app.state.store.log(
            model=body.model,
            provider_model=body.model,
            status_code=200,
            latency_ms=latency,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            estimated_cost=0,
            fallback_used=0,
            error=None,
        )
        await request.app.state.store.update_provider(provider_name, "healthy")
        return response

    except HTTPException:
        raise
    except AuthenticationRequiredError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        msg = str(exc)
        await request.app.state.store.update_provider(provider_name, "auth_failed", msg[:1000])
        await request.app.state.store.log(
            model=body.model,
            provider_model=body.model,
            status_code=401,
            latency_ms=latency,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost=0,
            fallback_used=0,
            error=msg[:1000],
        )
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": msg,
                    "type": "authentication_required",
                    "code": "browser_session_required",
                    "action": "Run 'python start.py --login' to sign into your free web account in Chrome.",
                }
            },
        ) from exc
    except ProviderRateLimitError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        msg = str(exc)
        await request.app.state.store.update_provider(provider_name, "rate_limited", msg[:1000])
        await request.app.state.store.log(
            model=body.model,
            provider_model=body.model,
            status_code=429,
            latency_ms=latency,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost=0,
            fallback_used=0,
            error=msg[:1000],
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": msg,
                    "type": "rate_limit_exceeded",
                    "code": "provider_quota_exhausted",
                    "action": "Try another model (e.g. 'deepseek', 'chatgpt', or 'grok').",
                }
            },
        ) from exc
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        msg = str(exc)
        logger.error(f"Chat completion failed: {exc}", exc_info=True)
        await request.app.state.store.update_provider(provider_name, "unhealthy", msg[:1000])
        await request.app.state.store.log(
            model=body.model,
            provider_model=body.model,
            status_code=502,
            latency_ms=latency,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost=0,
            fallback_used=0,
            error=msg[:1000],
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "message": f"Provider request failed: {msg}",
                    "type": "provider_error",
                    "code": "browser_generation_failed",
                }
            },
        ) from exc
