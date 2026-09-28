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


class ChatGPTProvider(BaseBrowserProvider):
    name = "chatgpt"
    display_name = "ChatGPT (Free Tier)"
    home_url = "https://chatgpt.com"
    model_aliases = ["chatgpt", "chatgpt-4o", "gpt-4o", "gpt-4o-mini", "chatgpt-free", "gpt-4"]

    async def _get_page(self, context: BrowserContext) -> Page:
        if self._active_page and not self._active_page.is_closed():
            return self._active_page
        
        # Check if there is an existing ChatGPT tab
        for p in context.pages:
            if not p.is_closed() and "chatgpt.com" in p.url:
                self._active_page = p
                return p

        # Check if there is an empty/blank tab to reuse
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
            if "chatgpt.com" not in page.url:
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(0.5)

            # Check if login button is displayed
            login_el = await page.query_selector('button[data-testid="login-button"], a[href*="login"]')
            chat_el = await page.query_selector('#prompt-textarea, div[contenteditable="true"]')

            if chat_el and not login_el:
                return True, "Authenticated (ChatGPT session active)"
            if login_el:
                return False, "Not authenticated. Sign in with 'python start.py --login'"
            
            # If chat input is present even without explicit user profile, free tier is usable
            if chat_el:
                return True, "Ready (ChatGPT free tier accessible)"

            return False, "Login required at chatgpt.com"
        except Exception as e:
            logger.warning(f"ChatGPT auth check failed: {e}")
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
                # Navigate to a fresh chat
                if not page.url.startswith("https://chatgpt.com"):
                    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=25000)
                else:
                    # New chat button or direct nav
                    try:
                        new_chat_btn = await page.query_selector('a[href="/"], button[aria-label="New chat"]')
                        if new_chat_btn:
                            await new_chat_btn.click()
                        else:
                            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=20000)

                await asyncio.sleep(0.15)

                # Dismiss any popups or banners
                await self.dismiss_banners(page)

                # Locate visible input element (skipping wcDTda_fallbackTextarea)
                candidates = [
                    'textarea[aria-label*="Chat with ChatGPT"]',
                    'textarea[placeholder*="Ask ChatGPT"]',
                    'div#prompt-textarea',
                    '#prompt-textarea:not(.wcDTda_fallbackTextarea)',
                    'div[contenteditable="true"]',
                    'div.ProseMirror',
                    'textarea:not(.wcDTda_fallbackTextarea):not([aria-hidden="true"])',
                ]
                input_el = await self.find_visible_input(page, candidates, timeout=15.0)
                if not input_el:
                    raise AuthenticationRequiredError("ChatGPT input area not found. Please log in with 'python start.py --login'")

                # Safely focus and enter prompt
                await self.enter_text_safely(page, input_el, prompt)

                # Count existing assistant turns
                assistant_selector = '[data-message-author-role="assistant"], .agent-turn, div.markdown'
                existing_bubbles = await page.query_selector_all(assistant_selector)
                initial_count = len(existing_bubbles)
                logger.info(f"[{self.display_name}] Existing assistant responses: {initial_count}")

                # Click send or press Enter
                send_candidates = [
                    'button[data-testid="send-button"]:not([disabled])',
                    'button[aria-label*="Send message"]:not([disabled])',
                    'button[aria-label*="Send"]:not([disabled])',
                    'button[aria-label*="send"]:not([disabled])',
                    'button:has(svg):not([disabled])',
                ]
                await self.click_send_or_press_enter(page, input_el, send_candidates)

                start_time = time.time()
                last_len = 0
                idle_count = 0
                started_generating = False

                while time.time() - start_time < timeout:
                    await asyncio.sleep(0.06)

                    # Look for rate limit warnings
                    error_el = await page.query_selector('.text-token-text-error, div[role="alert"]')
                    if error_el:
                        err_text = await error_el.inner_text()
                        if "limit" in err_text.lower() or "too many" in err_text.lower():
                            raise ProviderRateLimitError(f"ChatGPT rate limit reached: {err_text}")

                    # Check for stop button (active generation)
                    stop_btn = await page.query_selector('button[data-testid="stop-button"], button[aria-label*="Stop"]')
                    is_generating = stop_btn is not None

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
                        elif not is_generating:
                            idle_count += 1
                            if idle_count >= 3:  # No new text and stop button gone
                                break
                    elif not is_generating and started_generating:
                        break

                if not started_generating:
                    # Final attempt to extract text
                    bubbles = await page.query_selector_all(assistant_selector)
                    if len(bubbles) > initial_count:
                        yield await bubbles[-1].inner_text()
                    else:
                        raise ProviderTimeoutError("ChatGPT did not produce a response within the timeout.")

            except Exception as e:
                logger.error(f"ChatGPT generation error: {e}", exc_info=True)
                raise
