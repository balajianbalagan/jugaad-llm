from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncGenerator

from app.browser.manager import BrowserManager
from app.browser.providers.base import (
    AuthenticationRequiredError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    format_messages,
)

logger = logging.getLogger(__name__)


class GatewayService:
    """Orchestrates browser-based free tier LLMs and optional LiteLLM API fallbacks."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        browser_manager: BrowserManager | None = None,
        enable_api_keys: bool = False,
    ):
        self.config = config or {}
        self.enable_api_keys = enable_api_keys
        self.model_list = self.config.get("model_list", []) if enable_api_keys else []
        self.browser = browser_manager or BrowserManager()
        
        self.router = None
        if self.enable_api_keys and self.model_list:
            try:
                from litellm import Router
                self.router = Router(model_list=self.model_list, **self.config.get("router_settings", {}))
            except Exception as e:
                logger.warning(f"Could not initialize LiteLLM Router: {e}")

    @property
    def models(self) -> list[dict[str, Any]]:
        """List all available models: browser free-tier models and any API models."""
        result: list[dict[str, Any]] = []
        # Browser models
        result.extend(self.browser.supported_models)
        # API models if enabled
        if self.router and self.model_list:
            for item in self.model_list:
                m_name = item["model_name"]
                if not any(m["id"] == m_name for m in result):
                    result.append({
                        "id": m_name,
                        "object": "model",
                        "owned_by": item["litellm_params"]["model"].split("/", 1)[0],
                    })
        return result

    def is_browser_model(self, model_name: str) -> bool:
        """Check if model maps to a browser provider."""
        key = model_name.strip().lower()
        if key in ("auto", "default", "free"):
            return True
        return key in self.browser._model_map or any(name in key for name in self.browser.providers)

    async def complete(self, payload: dict[str, Any]) -> Any:
        """Route completion request to browser session or API router."""
        model = payload.get("model", "auto")
        stream = payload.get("stream", False)
        messages = payload.get("messages", [])
        timeout = payload.get("timeout", 120)

        # Route to browser if it's a browser model or router is not active
        if not self.router or self.is_browser_model(model):
            if stream:
                return self._stream_browser_completion(model, messages, timeout)
            else:
                return await self._sync_browser_completion(model, messages, timeout)

        # Fallback / Route to LiteLLM router
        return await self.router.acompletion(**payload)

    async def _stream_browser_completion(
        self,
        model: str,
        messages: list[Any],
        timeout: int = 120
    ) -> AsyncGenerator[dict[str, Any], None]:
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        # Yield initial empty role chunk
        yield {
            "id": cmpl_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
        }

        async for chunk in self.browser.complete_stream(model, messages, timeout=timeout):
            yield {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]
            }

        # Yield final stop chunk
        yield {
            "id": cmpl_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }

    async def _sync_browser_completion(
        self,
        model: str,
        messages: list[Any],
        timeout: int = 120
    ) -> dict[str, Any]:
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        chunks = []

        async for chunk in self.browser.complete_stream(model, messages, timeout=timeout):
            chunks.append(chunk)

        full_content = "".join(chunks)
        if not full_content.strip():
            raise ProviderTimeoutError(
                f"[{model}] Provider session returned empty or whitespace-only response. "
                f"Please verify session login via 'python start.py --login' or test with another model."
            )
        prompt_text = format_messages(messages)
        prompt_tokens = max(1, len(prompt_text.split()))
        completion_tokens = max(1, len(full_content.split()))

        return {
            "id": cmpl_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": full_content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
