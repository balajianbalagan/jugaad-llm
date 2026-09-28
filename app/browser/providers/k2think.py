from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncGenerator

from playwright.async_api import BrowserContext, Page

from app.browser.providers.base import (
    AuthenticationRequiredError,
    BaseBrowserProvider,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

logger = logging.getLogger(__name__)


class K2ThinkProvider(BaseBrowserProvider):
    name = "k2think"
    display_name = "K2 Think (Reasoning)"
    home_url = "https://chat.ifm.ai/"
    model_aliases = [
        "k2think",
        "k2-think",
        "k2",
        "k2think-v2",
        "k2-reasoner",
        "k2think-32b",
    ]

    async def _get_page(self, context: BrowserContext) -> Page:
        if self._active_page and not self._active_page.is_closed():
            return self._active_page

        for p in context.pages:
            if not p.is_closed() and ("ifm.ai" in p.url or "k2think.ai" in p.url):
                self._active_page = p
                return p

        for p in context.pages:
            if not p.is_closed() and p.url in ("about:blank", "chrome://newtab/"):
                self._active_page = p
                return p

        page = await context.new_page()
        self._active_page = page
        return page

    async def check_auth(self, context: BrowserContext) -> tuple[bool, str]:
        try:
            page = await self._get_page(context)
            if "ifm.ai" not in page.url and "k2think.ai" not in page.url:
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(1.5)

            await self.dismiss_banners(page)
            # Wait for SPA to hydrate
            try:
                chat_input = await page.wait_for_selector(
                    '#chat-input, textarea[placeholder*="Ask K2"], textarea[placeholder*="Horizon"], textarea',
                    timeout=8000,
                )
                if chat_input and await chat_input.is_visible():
                    return True, "Ready (K2 Horizon reasoning accessible)"
            except Exception:
                pass

            # Fallback cookie check
            cookies = await context.cookies()
            has_token = any("ifm.ai" in c.get("domain", "") and c.get("name") == "token" for c in cookies)
            if has_token:
                return True, "Authenticated (K2 session active)"

            return False, "K2 Think input area not found (sign-in required)"
        except Exception as e:
            logger.warning(f"K2 Think auth check failed: {e}")
            return False, f"Check failed: {e}"

    async def generate(
        self,
        context: BrowserContext,
        prompt: str,
        stream: bool = False,
        timeout: int = 120,
    ) -> AsyncGenerator[str, None]:
        async with self._lock:
            page = await self._get_page(context)
            try:
                logger.info(f"[{self.display_name}] Preparing K2 Think session...")
                if "ifm.ai" not in page.url and "k2think.ai" not in page.url:
                    await page.goto(self.home_url, wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(1.5)

                await self.dismiss_banners(page)

                candidates = [
                    '#chat-input',
                    'textarea[placeholder*="Ask K2"]',
                    'textarea[placeholder*="Horizon"]',
                    'textarea[placeholder*="Anything"]',
                    '#prompt-textarea',
                    'textarea',
                ]
                input_el = await self.find_visible_input(page, candidates, timeout=15.0)
                if not input_el:
                    raise AuthenticationRequiredError("K2 Think input element not found.")

                await self.enter_text_safely(page, input_el, prompt)

                assistant_selector = 'div.prose, div[class*="message"], div[class*="markdown"], div[class*="bubble"], div[class*="response"]'
                existing_bubbles = await page.query_selector_all(assistant_selector)
                initial_count = len(existing_bubbles)
                logger.info(f"[{self.display_name}] Existing assistant responses: {initial_count}")

                send_candidates = [
                    '#send-message-button',
                    'button[type="submit"]:has(svg):not([disabled])',
                    'button[aria-label*="Send"]:not([disabled])',
                    'button[type="submit"]:not([disabled])',
                    'button:has(svg):not([disabled])',
                ]
                await self.click_send_or_press_enter(page, input_el, send_candidates)

                logger.info(f"[{self.display_name}] Waiting for K2 Think response stream...")
                start_time = time.time()
                last_len = 0
                idle_count = 0
                started_generating = False

                while time.time() - start_time < timeout:
                    await asyncio.sleep(0.2)

                    bubbles = await page.query_selector_all(assistant_selector)
                    if len(bubbles) > initial_count:
                        started_generating = True
                        latest_bubble = bubbles[-1]
                        current_text = await latest_bubble.inner_text()

                        if len(current_text) > last_len:
                            chunk = current_text[last_len:]
                            last_len = len(current_text)
                            idle_count = 0
                            yield chunk
                        else:
                            idle_count += 1
                            if idle_count >= 5:
                                break
                    elif not started_generating and bubbles and not existing_bubbles:
                        started_generating = True
                        latest_bubble = bubbles[-1]
                        current_text = await latest_bubble.inner_text()
                        if len(current_text) > last_len:
                            chunk = current_text[last_len:]
                            last_len = len(current_text)
                            yield chunk

                if not started_generating:
                    bubbles = await page.query_selector_all(assistant_selector)
                    if len(bubbles) > initial_count:
                        yield await bubbles[-1].inner_text()
                    else:
                        raise ProviderTimeoutError("K2 Think did not produce a response within the timeout.")

            except Exception as e:
                logger.error(f"K2 Think generation error: {e}", exc_info=True)
                raise
