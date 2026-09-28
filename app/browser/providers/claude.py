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


class ClaudeProvider(BaseBrowserProvider):
    name = "claude"
    display_name = "Claude (Free Tier)"
    home_url = "https://claude.ai/new"
    model_aliases = [
        "claude",
        "claude-3-5-sonnet",
        "claude-3-sonnet",
        "claude-3-5-haiku",
        "claude-haiku",
        "claude-free",
    ]

    async def _get_page(self, context: BrowserContext) -> Page:
        if self._active_page and not self._active_page.is_closed():
            return self._active_page
        
        for p in context.pages:
            if not p.is_closed() and "claude.ai" in p.url:
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
            if "claude.ai" not in page.url:
                await page.goto("https://claude.ai/new", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(0.5)

            current_url = page.url
            if "login" in current_url:
                return False, "Not authenticated. Sign in with 'python start.py --login'"

            chat_input = await page.query_selector('div.ProseMirror, fieldset div[contenteditable="true"]')
            login_btn = await page.query_selector('input[type="email"], button:has-text("Continue with Google")')

            if chat_input and not login_btn:
                return True, "Authenticated (Claude session active)"
            if login_btn:
                return False, "Login required at claude.ai"

            return False, "Claude session not detected"
        except Exception as e:
            logger.warning(f"Claude auth check failed: {e}")
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
                # Open new chat
                await page.goto("https://claude.ai/new", wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(0.15)
                await self.wait_for_cloudflare_challenge(page, timeout=10.0)

                # Check if login is needed
                if "login" in page.url:
                    raise AuthenticationRequiredError("Claude requires login. Run 'python start.py --login'")

                # Dismiss cookie consent banner and popups
                await self.dismiss_banners(page)

                # Locate visible input element
                candidates = [
                    'div.ProseMirror',
                    'fieldset div[contenteditable="true"]',
                    'div[contenteditable="true"]',
                    'div[role="textbox"]',
                    'textarea',
                ]
                input_el = await self.find_visible_input(page, candidates, timeout=15.0)
                if not input_el:
                    if "login" in page.url:
                        raise AuthenticationRequiredError("Claude requires login. Run 'python start.py --login'")
                    raise AuthenticationRequiredError("Claude input field not found. Please log in with 'python start.py --login'")

                # Safely enter prompt
                await self.enter_text_safely(page, input_el, prompt)

                # Count existing bubbles
                assistant_selector = '.font-claude-message, [data-is-streaming], div.font-user-message ~ div, .standard-markdown'
                existing_bubbles = await page.query_selector_all(assistant_selector)
                initial_count = len(existing_bubbles)

                # Send button or enter
                send_candidates = [
                    'fieldset button[aria-label*="Send"]:not([disabled])',
                    'button[aria-label="Send Message"]:not([disabled])',
                    'button[aria-label="Send message"]:not([disabled])',
                    'button[aria-label*="Send"]:not([disabled])',
                    'fieldset button:has(svg):not([disabled])',
                ]
                await self.click_send_or_press_enter(page, input_el, send_candidates)

                start_time = time.time()
                last_len = 0
                idle_count = 0
                started_generating = False

                while time.time() - start_time < timeout:
                    await asyncio.sleep(0.06)

                    # Check for rate limit banner
                    page_text = await page.inner_text('body')
                    if "out of free messages until" in page_text or "message limit reached" in page_text.lower():
                        raise ProviderRateLimitError("Claude hourly free quota exceeded. Fallback recommended.")

                    stop_btn = await page.query_selector('button[aria-label*="Stop response"], button[aria-label*="Stop"]')
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
                            if idle_count >= 3:
                                break
                    elif not started_generating and bubbles and not existing_bubbles:
                        started_generating = True
                        latest_bubble = bubbles[-1]
                        current_text = await latest_bubble.inner_text()
                        if len(current_text) > last_len:
                            chunk = current_text[last_len:]
                            last_len = len(current_text)
                            yield chunk
                    elif not is_generating and started_generating:
                        break

                if not started_generating:
                    bubbles = await page.query_selector_all(assistant_selector)
                    if len(bubbles) > initial_count:
                        yield await bubbles[-1].inner_text()
                    else:
                        raise ProviderTimeoutError("Claude did not produce a response within the timeout.")

            except Exception as e:
                logger.error(f"Claude generation error: {e}", exc_info=True)
                raise
