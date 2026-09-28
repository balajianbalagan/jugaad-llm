from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import AsyncGenerator

from playwright.async_api import BrowserContext, Page

from app.browser.providers.base import (
    AuthenticationRequiredError,
    BaseBrowserProvider,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

logger = logging.getLogger(__name__)


class OllamaProvider(BaseBrowserProvider):
    name = "ollama"
    display_name = "Ollama (Cloud & Local)"
    home_url = "http://localhost:11434"
    model_aliases = [
        "ollama",
        "ollama-cloud",
        "llama3",
        "llama3.1",
        "llama3.2",
        "llama3.3",
        "qwen2.5",
        "deepseek-r1-ollama",
        "mistral-ollama",
        "phi3",
    ]

    def _get_endpoint(self) -> str:
        return (
            os.environ.get("OLLAMA_API_BASE")
            or os.environ.get("OLLAMA_HOST")
            or "http://localhost:11434"
        ).rstrip("/")

    def _get_api_key(self) -> str | None:
        return os.environ.get("OLLAMA_API_KEY")

    async def _get_page(self, context: BrowserContext) -> Page:
        # Fallback to base page if needed
        return context.pages[0] if context.pages else await context.new_page()

    async def check_auth(self, context: BrowserContext) -> tuple[bool, str]:
        endpoint = self._get_endpoint()
        try:
            # Check native Ollama /api/version or /api/tags
            url = f"{endpoint}/api/tags"
            req = urllib.request.Request(url, method="GET")
            api_key = self._get_api_key()
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")

            def _ping():
                with urllib.request.urlopen(req, timeout=3) as resp:
                    return json.loads(resp.read().decode())

            data = await asyncio.to_thread(_ping)
            models = [m.get("name") for m in data.get("models", [])]
            return True, f"Ready (Ollama running at {endpoint}, {len(models)} models available)"
        except Exception:
            if self._get_api_key():
                return True, "Ready (Ollama Cloud API Key configured)"
            return False, f"Ollama not running at {endpoint} (or no OLLAMA_API_KEY set)"

    async def generate(
        self,
        context: BrowserContext,
        prompt: str,
        stream: bool = False,
        timeout: int = 120,
    ) -> AsyncGenerator[str, None]:
        endpoint = self._get_endpoint()
        model_name = os.environ.get("OLLAMA_MODEL", "llama3.2")
        url = f"{endpoint}/api/generate"
        payload = {
            "model": model_name,
            "prompt": prompt,
            "stream": True,
        }
        headers = {"Content-Type": "application/json"}
        api_key = self._get_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")

            def _open():
                return urllib.request.urlopen(req, timeout=timeout)

            resp = await asyncio.to_thread(_open)

            loop = asyncio.get_running_loop()

            def _read_chunk():
                line = resp.readline()
                if not line:
                    return None
                try:
                    data = json.loads(line.decode("utf-8"))
                    return data.get("response", ""), data.get("done", False)
                except Exception:
                    return "", False

            while True:
                chunk_data = await asyncio.to_thread(_read_chunk)
                if chunk_data is None:
                    break
                text_part, done = chunk_data
                if text_part:
                    yield text_part
                if done:
                    break

        except urllib.error.URLError as e:
            raise AuthenticationRequiredError(
                f"Could not connect to Ollama at {endpoint}: {e}. Ensure Ollama is running or set OLLAMA_HOST/OLLAMA_API_KEY."
            ) from e
        except Exception as e:
            logger.error(f"Ollama generation error: {e}", exc_info=True)
            raise
