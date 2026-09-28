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


class PoeProvider(BaseBrowserProvider):
    name = "poe"
    display_name = "Poe (Free Tier)"
    home_url = "https://poe.com/Assistant"
    model_aliases = [
        "poe",
        "poe-chat",
        "poe-assistant",
        "assistant-poe",
        "poe-gpt4o",
        "poe-claude",
    ]

    async def _get_page(self, context: BrowserContext) -> Page:
        if self._active_page and not self._active_page.is_closed():
            return self._active_page

        for p in context.pages:
            if not p.is_closed() and "poe.com" in p.url:
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
            if "poe.com" not in page.url:
                logger.info(f"[{self.display_name}] Checking auth, navigating to {self.home_url}...")
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(1.5)

            await self.dismiss_banners(page)

            # If redirected to login page
            if "/login" in page.url:
                return False, "Login required at poe.com"

            try:
                chat_input = await page.wait_for_selector(
                    'textarea[placeholder*="Message"], textarea[class*="GrowingTextArea"], textarea[placeholder*="Talk to"], textarea',
                    timeout=5000,
                )
                if chat_input and await chat_input.is_visible():
                    login_modal = await page.query_selector('div[class*="LoginModal"]')
                    if not (login_modal and await login_modal.is_visible()):
                        logger.info(f"[{self.display_name}] Auth check: Poe chat input is accessible.")
                        return True, "Ready (Poe session active)"
            except Exception:
                pass

            return False, "Poe session not detected (sign-in required)"
        except Exception as e:
            logger.warning(f"Poe auth check failed: {e}")
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
                logger.info(f"[{self.display_name}] Preparing Poe chat session...")
                if "poe.com" not in page.url or page.url.endswith("poe.com/"):
                    await page.goto("https://poe.com/Assistant", wait_until="domcontentloaded", timeout=25000)
                else:
                    try:
                        new_chat = await page.query_selector('button[aria-label*="Clear context"], button[aria-label*="New chat"]')
                        if new_chat and await new_chat.is_visible():
                            await new_chat.click()
                    except Exception:
                        pass

                await asyncio.sleep(1.5)
                await self.dismiss_banners(page)

                candidates = [
                    'textarea[placeholder*="Message"]',
                    'textarea[class*="GrowingTextArea"]',
                    'textarea[placeholder*="Talk to"]',
                    'textarea[class*="ChatMessageInput"]',
                    'textarea',
                    'div[contenteditable="true"]',
                ]
                input_el = await self.find_visible_input(page, candidates, timeout=15.0)
                if not input_el:
                    raise AuthenticationRequiredError("Poe input field not found. Please log in with 'python start.py --login'")

                await self.enter_text_safely(page, input_el, prompt)

                assistant_selector = 'div[class*="Message_botMessageBubble"], div[class*="ChatMessage_messageWrapper"], div.Markdown_markdownContainer'
                existing_bubbles = await page.query_selector_all(assistant_selector)
                initial_count = len(existing_bubbles)
                logger.info(f"[{self.display_name}] Existing message bubbles: {initial_count}")

                send_candidates = [
                    'button[aria-label="Send message"]:not([disabled])',
                    'button[class*="ChatMessageSendButton"]:not([disabled])',
                    'button[aria-label*="Send"]:not([disabled])',
                    'button:has(svg):not([disabled])',
                ]
                await self.click_send_or_press_enter(page, input_el, send_candidates)

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
                        raise ProviderTimeoutError("Poe did not produce a response within the timeout.")

            except Exception as e:
                logger.error(f"Poe generation error: {e}", exc_info=True)
                raise
