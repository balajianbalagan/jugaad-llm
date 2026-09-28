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


class HuggingFaceProvider(BaseBrowserProvider):
    name = "huggingface"
    display_name = "HuggingFace Chat (Free Tier)"
    home_url = "https://huggingface.co/chat"
    model_aliases = [
        "huggingface",
        "hf",
        "huggingchat",
        "hf-chat",
        "command-r",
        "llama-3-hf",
        "qwen-hf",
    ]

    async def _get_page(self, context: BrowserContext) -> Page:
        if self._active_page and not self._active_page.is_closed():
            return self._active_page

        for p in context.pages:
            if not p.is_closed() and "huggingface.co/chat" in p.url:
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
            if "huggingface.co/chat" not in page.url:
                logger.info(f"[{self.display_name}] Checking auth, navigating to {self.home_url}...")
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)

            await self.dismiss_banners(page)
            await asyncio.sleep(1.0)
            chat_input = await page.query_selector('textarea[placeholder*="Ask"], textarea, div[contenteditable="true"]')
            if chat_input and await chat_input.is_visible():
                logger.info(f"[{self.display_name}] Auth check: HuggingChat input textarea active.")
                return True, "Ready (HuggingFace Chat accessible)"
            return False, "HuggingFace Chat input not detected"
        except Exception as e:
            logger.warning(f"HuggingFace auth check failed: {e}")
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
                logger.info(f"[{self.display_name}] Preparing HuggingChat session...")
                if not page.url.startswith("https://huggingface.co/chat"):
                    await page.goto("https://huggingface.co/chat", wait_until="domcontentloaded", timeout=25000)
                else:
                    try:
                        new_chat = await page.query_selector('a[href="/chat/"], a[href="/chat"]')
                        if new_chat and await new_chat.is_visible():
                            await new_chat.click()
                        else:
                            await page.goto("https://huggingface.co/chat", wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        await page.goto("https://huggingface.co/chat", wait_until="domcontentloaded", timeout=20000)

                await asyncio.sleep(0.15)
                await self.dismiss_banners(page)

                candidates = [
                    'textarea[placeholder*="Ask"]',
                    'textarea[placeholder*="message"]',
                    'textarea[name="message"]',
                    'textarea',
                    'div[contenteditable="true"]',
                ]
                input_el = await self.find_visible_input(page, candidates, timeout=15.0)
                if not input_el:
                    raise AuthenticationRequiredError("HuggingFace Chat input area not found.")

                await self.enter_text_safely(page, input_el, prompt)

                assistant_selector = 'div.prose, div[class*="message"], div[class*="prose"]'
                existing_bubbles = await page.query_selector_all(assistant_selector)
                initial_count = len(existing_bubbles)
                logger.info(f"[{self.display_name}] Existing assistant responses: {initial_count}")

                send_candidates = [
                    'button[aria-label*="Send message"]:not([disabled])',
                    'button[aria-label*="Send"]:not([disabled])',
                    'button[type="submit"]:not([disabled])',
                    'button[aria-label*="Submit"]:not([disabled])',
                    'button:has(svg):not([disabled])',
                ]
                await self.click_send_or_press_enter(page, input_el, send_candidates)

                logger.info(f"[{self.display_name}] Waiting for HuggingChat response stream...")
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
                        raise ProviderTimeoutError("HuggingFace Chat did not produce a response within the timeout.")

            except Exception as e:
                logger.error(f"HuggingFace Chat generation error: {e}", exc_info=True)
                raise
