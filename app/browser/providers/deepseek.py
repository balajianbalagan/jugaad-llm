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


class DeepSeekProvider(BaseBrowserProvider):
    name = "deepseek"
    display_name = "DeepSeek (Free Tier)"
    home_url = "https://chat.deepseek.com"
    model_aliases = [
        "deepseek",
        "deepseek-chat",
        "deepseek-v3",
        "deepseek-r1",
        "deepseek-reasoner",
        "deepseek-free",
    ]

    async def _get_page(self, context: BrowserContext) -> Page:
        if self._active_page and not self._active_page.is_closed():
            return self._active_page

        for p in context.pages:
            if not p.is_closed() and "chat.deepseek.com" in p.url:
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
            if "chat.deepseek.com" not in page.url:
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(0.5)

            if "sign_in" in page.url:
                return False, "Not authenticated. Sign in with 'python start.py --login'"

            chat_input = await page.query_selector('#chat-input, textarea')
            login_inputs = await page.query_selector('input[type="password"], input[placeholder*="Phone"]')

            if chat_input and not login_inputs:
                return True, "Authenticated (DeepSeek session active)"
            if login_inputs:
                return False, "Login required at chat.deepseek.com"

            return False, "DeepSeek session not detected"
        except Exception as e:
            logger.warning(f"DeepSeek auth check failed: {e}")
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
                if not page.url.startswith("https://chat.deepseek.com"):
                    await page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=25000)
                else:
                    try:
                        new_chat = await page.query_selector('div:has-text("New chat"), div[role="button"]:has-text("New")')
                        if new_chat:
                            await new_chat.click()
                        else:
                            await page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        await page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=20000)

                await asyncio.sleep(0.15)

                if "sign_in" in page.url:
                    raise AuthenticationRequiredError("DeepSeek requires login. Run 'python start.py --login'")

                # Dismiss any popups or banners
                await self.dismiss_banners(page)

                # If search dialog is open from previous action, close it
                close_search_btn = await page.query_selector('div[role="dialog"] button, button[aria-label*="Close"]')
                if close_search_btn and await close_search_btn.is_visible():
                    try:
                        await close_search_btn.click()
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass

                # Locate visible input element
                candidates = [
                    '#chat-input',
                    'textarea[placeholder*="DeepSeek"]',
                    'textarea:not([aria-hidden="true"])',
                    'div[contenteditable="true"]',
                ]
                input_el = await self.find_visible_input(page, candidates, timeout=15.0)
                if not input_el:
                    raise AuthenticationRequiredError("DeepSeek input element not found. Please log in with 'python start.py --login'")

                # Safely enter prompt
                await self.enter_text_safely(page, input_el, prompt)

                # Count existing assistant bubbles
                assistant_selector = '.ds-markdown, .chat-message-assistant, div[class*="markdown"], .ds-message'
                existing_bubbles = await page.query_selector_all(assistant_selector)
                initial_count = len(existing_bubbles)

                # Find specific send button (NEVER click search/think buttons!)
                send_btn = None
                for sel in [
                    'div[role="button"][aria-label*="Send"]',
                    'button[aria-label*="Send"]',
                    'div[role="button"][aria-label*="send"]',
                    'button[aria-label*="send"]',
                    'div[class*="sendBtn"]',
                    'div[class*="send-btn"]',
                    'div[class*="send_btn"]',
                ]:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        aria = (await btn.get_attribute("aria-label") or "").lower()
                        text = (await btn.inner_text() or "").lower()
                        if "search" not in aria and "search" not in text and "think" not in aria:
                            send_btn = btn
                            break

                if send_btn:
                    await send_btn.click()
                else:
                    await input_el.press("Enter")

                start_time = time.time()
                last_len = 0
                last_change_time = time.time()
                started_generating = False

                while time.time() - start_time < timeout:
                    await asyncio.sleep(0.06)

                    bubbles = await page.query_selector_all(assistant_selector)
                    if len(bubbles) > initial_count:
                        latest_bubble = bubbles[-1]
                        current_text = await latest_bubble.inner_text()

                        if len(current_text) > last_len:
                            chunk = current_text[last_len:]
                            last_len = len(current_text)
                            last_change_time = time.time()
                            if not started_generating and current_text.strip():
                                started_generating = True
                            yield chunk
                        elif started_generating and last_len > 0:
                            if time.time() - last_change_time >= 1.8:
                                break
                    elif not started_generating and bubbles and not existing_bubbles:
                        latest_bubble = bubbles[-1]
                        current_text = await latest_bubble.inner_text()
                        if len(current_text) > last_len:
                            chunk = current_text[last_len:]
                            last_len = len(current_text)
                            last_change_time = time.time()
                            if not started_generating and current_text.strip():
                                started_generating = True
                            yield chunk
                        elif started_generating and last_len > 0:
                            if time.time() - last_change_time >= 1.8:
                                break

                if not started_generating or last_len == 0:
                    bubbles = await page.query_selector_all(assistant_selector)
                    if len(bubbles) > initial_count:
                        final_text = (await bubbles[-1].inner_text()).strip()
                        if final_text:
                            yield final_text
                            return
                    raise ProviderTimeoutError("DeepSeek did not produce a response within the timeout.")

            except Exception as e:
                logger.error(f"DeepSeek generation error: {e}", exc_info=True)
                raise
