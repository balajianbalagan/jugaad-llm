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


class GrokProvider(BaseBrowserProvider):
    name = "grok"
    display_name = "Grok (Free Tier)"
    home_url = "https://grok.com"
    model_aliases = [
        "grok",
        "grok-2",
        "grok-3",
        "grok-mini",
        "grok-beta",
        "grok-free",
    ]

    async def _get_page(self, context: BrowserContext) -> Page:
        if self._active_page and not self._active_page.is_closed():
            return self._active_page

        for p in context.pages:
            if not p.is_closed() and "grok.com" in p.url:
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
            if "grok.com" not in page.url:
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)

            chat_input = await page.query_selector('textarea, input[type="text"]')
            if chat_input:
                return True, "Ready (Grok interface accessible)"

            return True, "Grok accessible"
        except Exception as e:
            logger.warning(f"Grok auth check failed: {e}")
            return False, f"Check failed: {e}"

    async def generate(
        self,
        context: BrowserContext,
        prompt: str,
        stream: bool = False,
        timeout: int = 120
    ) -> AsyncGenerator[str, None]:
        async with self._lock:
            page = await self._get_page(context)
            try:
                if not page.url.startswith("https://grok.com"):
                    await page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=25000)
                else:
                    await page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=20000)

                await asyncio.sleep(0.15)

                # Dismiss any popups or banners
                await self.dismiss_banners(page)

                # Locate visible input (skipping aria-hidden and tabindex=-1 shadow textareas)
                candidates = [
                    'textarea[aria-label*="Ask Grok"]',
                    'textarea.prose',
                    'textarea:not([aria-hidden="true"]):not([tabindex="-1"])',
                    'div[role="textbox"]',
                    'div[contenteditable="true"]',
                    'textarea',
                    'input[type="text"]',
                ]
                input_el = await self.find_visible_input(page, candidates, timeout=15.0)
                if not input_el:
                    raise AuthenticationRequiredError("Grok input field not found. Please log in with 'python start.py --login'")

                # Safely focus and enter prompt
                await self.enter_text_safely(page, input_el, prompt)

                # Count existing assistant turns to avoid reading old messages
                assistant_selector = '.message-bubble, div[class*="response"], .markdown, div[class*="bubble"], div[class*="prose"], div[dir="auto"]'
                existing_bubbles = await page.query_selector_all(assistant_selector)
                initial_count = len(existing_bubbles)
                logger.info(f"[{self.display_name}] Existing assistant responses: {initial_count}")

                # Submit via send button if available or Enter
                send_candidates = [
                    'button[aria-label*="Submit"]:not([disabled])',
                    'button[aria-label*="Send"]:not([disabled])',
                    'button[type="submit"]:not([disabled])',
                    'button:has(svg):not([disabled])',
                ]
                await self.click_send_or_press_enter(page, input_el, send_candidates)

                start_time = time.time()
                last_len = 0
                idle_count = 0
                started_generating = False

                while time.time() - start_time < timeout:
                    await asyncio.sleep(0.06)

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
                            if idle_count >= 4:
                                break
                    elif not started_generating and bubbles and not existing_bubbles:
                        # First bubble in empty chat
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
                        raise ProviderTimeoutError("Grok did not produce a response within the timeout.")

            except Exception as e:
                logger.error(f"Grok generation error: {e}", exc_info=True)
                raise
