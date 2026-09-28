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


class GeminiProvider(BaseBrowserProvider):
    name = "gemini"
    display_name = "Google Gemini (Free Tier)"
    home_url = "https://gemini.google.com/app"
    model_aliases = [
        "gemini",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-2.0-flash",
        "gemini-pro",
        "gemini-flash",
        "gemini-free",
    ]

    async def _get_page(self, context: BrowserContext) -> Page:
        if self._active_page and not self._active_page.is_closed():
            return self._active_page

        for p in context.pages:
            if not p.is_closed() and "gemini.google.com" in p.url:
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
            if "gemini.google.com" not in page.url:
                logger.info(f"[{self.display_name}] Checking auth, navigating to {self.home_url}...")
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(0.5)

            await self.dismiss_banners(page)

            if "accounts.google.com" in page.url or "signin" in page.url:
                logger.info(f"[{self.display_name}] Auth check: Redirected to Google Sign-in.")
                return False, "Not authenticated with Google. Run 'python start.py --login'"

            chat_input = await page.query_selector('div.ql-editor, div[contenteditable="true"], rich-textarea, div[aria-label*="prompt"]')
            if chat_input and await chat_input.is_visible():
                logger.info(f"[{self.display_name}] Auth check: Active chat input detected. Session is authenticated.")
                return True, "Authenticated (Gemini session active)"

            sign_in_page = await page.query_selector('a[href*="accounts.google.com/signin"], button:has-text("Sign in to Gemini")')
            if sign_in_page and await sign_in_page.is_visible():
                logger.info(f"[{self.display_name}] Auth check: Primary sign-in call to action visible.")
                return False, "Login required at gemini.google.com"

            if chat_input:
                return True, "Authenticated (Gemini input found)"

            return False, "Gemini session not detected"
        except Exception as e:
            logger.warning(f"Gemini auth check failed: {e}")
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
                logger.info(f"[{self.display_name}] Preparing page for prompt generation...")
                if not page.url.startswith("https://gemini.google.com"):
                    await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=25000)
                else:
                    try:
                        new_chat = await page.query_selector('button[aria-label*="New chat"]')
                        if new_chat and await new_chat.is_visible():
                            await new_chat.click()
                        else:
                            await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=20000)

                await asyncio.sleep(0.15)
                await self.dismiss_banners(page)

                if "accounts.google.com" in page.url or "signin" in page.url:
                    raise AuthenticationRequiredError("Gemini requires Google login. Run 'python start.py --login'")

                candidates = [
                    'div.ql-editor',
                    'div[aria-label*="Enter a prompt"]',
                    'div[aria-label*="prompt"]',
                    'div.ql-editor.textarea',
                    'div.ql-editor.new-input-ui',
                    'rich-textarea .ql-editor',
                    'div[contenteditable="true"][role="textbox"]',
                    'div[contenteditable="true"]',
                    'textarea[aria-label*="prompt"]',
                    'rich-textarea textarea',
                    'div[role="textbox"]',
                ]
                input_el = await self.find_visible_input(page, candidates, timeout=15.0)
                if not input_el:
                    raise AuthenticationRequiredError("Gemini input field not found. Please log in with 'python start.py --login'")

                # Safely enter text into Quill editor
                await self.enter_text_safely(page, input_el, prompt)

                # Count existing assistant turns
                assistant_selector = 'model-response, message-content, .model-response-text, .response-container, div.markdown'
                existing_bubbles = await page.query_selector_all(assistant_selector)
                initial_count = len(existing_bubbles)
                logger.info(f"[{self.display_name}] Existing assistant responses: {initial_count}")

                # Click send or press Enter
                send_candidates = [
                    'button[aria-label*="Send prompt"]:not([disabled])',
                    'button[aria-label*="Send message"]:not([disabled])',
                    'button[aria-label*="Send"]:not([disabled])',
                    'button.send-button:not([disabled])',
                    'button:has(mat-icon[data-mat-icon-name="send"]):not([disabled])',
                    'button:has(mat-icon):not([disabled])',
                ]
                await self.click_send_or_press_enter(page, input_el, send_candidates)

                logger.info(f"[{self.display_name}] Waiting for response stream...")
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
                        raw_text = await latest_bubble.inner_text()
                        # Strip Gemini screen-reader announcer prefix if present
                        current_text = raw_text
                        if current_text.startswith("Gemini said"):
                            current_text = current_text[len("Gemini said"):].lstrip()

                        if len(current_text) > last_len:
                            chunk = current_text[last_len:]
                            last_len = len(current_text)
                            idle_count = 0
                            yield chunk
                        else:
                            idle_count += 1
                            if idle_count >= 6:  # Gemini stream finished
                                break
                    elif not started_generating and bubbles and not existing_bubbles:
                        started_generating = True
                        latest_bubble = bubbles[-1]
                        raw_text = await latest_bubble.inner_text()
                        current_text = raw_text
                        if current_text.startswith("Gemini said"):
                            current_text = current_text[len("Gemini said"):].lstrip()
                        if len(current_text) > last_len:
                            chunk = current_text[last_len:]
                            last_len = len(current_text)
                            yield chunk

                if not started_generating:
                    bubbles = await page.query_selector_all(assistant_selector)
                    if len(bubbles) > initial_count:
                        yield await bubbles[-1].inner_text()
                    else:
                        raise ProviderTimeoutError("Gemini did not produce a response within the timeout.")

            except Exception as e:
                logger.error(f"Gemini generation error: {e}", exc_info=True)
                raise
