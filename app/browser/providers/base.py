from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)


class BrowserProviderError(Exception):
    """Base exception for browser provider errors."""
    pass


class AuthenticationRequiredError(BrowserProviderError):
    """Raised when provider requires manual login/authentication."""
    pass


class ProviderRateLimitError(BrowserProviderError):
    """Raised when free-tier rate limits or quotas are hit."""
    pass


class ProviderTimeoutError(BrowserProviderError):
    """Raised when response generation times out."""
    pass


def format_messages(messages: list[Any]) -> str:
    """Format an OpenAI-style list of messages into a single chat prompt."""
    if not messages:
        return ""
    
    # Normalize message items
    parsed: list[dict[str, str]] = []
    for m in messages:
        if hasattr(m, "model_dump"):
            d = m.model_dump()
        elif hasattr(m, "dict"):
            d = m.dict()
        elif isinstance(m, dict):
            d = m
        else:
            d = {"role": "user", "content": str(m)}
        role = d.get("role", "user")
        content = d.get("content", "")
        if isinstance(content, list):
            # Extract text parts if multi-part
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and "text" in part]
            content = " ".join(text_parts) if text_parts else str(content)
        parsed.append({"role": role, "content": str(content)})

    # Single user message shortcut
    if len(parsed) == 1 and parsed[0]["role"] == "user":
        return parsed[0]["content"]

    # System + single user message
    if len(parsed) == 2 and parsed[0]["role"] in ("system", "developer") and parsed[1]["role"] == "user":
        return f"[System Instructions]\n{parsed[0]['content']}\n\n[User Prompt]\n{parsed[1]['content']}"

    # Multi-turn conversation
    lines = []
    system_prompts = [p["content"] for p in parsed if p["role"] in ("system", "developer")]
    if system_prompts:
        lines.append(f"[System Instructions]\n{'\n'.join(system_prompts)}\n")

    lines.append("[Conversation History]")
    for p in parsed:
        if p["role"] in ("system", "developer"):
            continue
        role_label = "User" if p["role"] == "user" else "Assistant"
        lines.append(f"{role_label}: {p['content']}")
    
    lines.append("\nAssistant:")
    return "\n".join(lines)


class BaseBrowserProvider(ABC):
    name: str = "base"
    display_name: str = "Base Provider"
    home_url: str = ""
    model_aliases: list[str] = []

    def __init__(self):
        self._lock = asyncio.Lock()
        self._active_page: Page | None = None

    async def wait_for_cloudflare_challenge(self, page: Page, timeout: float = 8.0) -> bool:
        """Detect and wait for Cloudflare Turnstile or security challenges to resolve."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                title = await page.title()
                if any(w in title.lower() for w in ["just a moment", "attention required", "cloudflare"]):
                    logger.info(f"[{self.display_name}] Cloudflare challenge detected, waiting for verification...")
                    for frame in page.frames:
                        try:
                            cb = await frame.query_selector('input[type="checkbox"], .ctp-checkbox-label, #challenge-stage')
                            if cb and await cb.is_visible():
                                await cb.click()
                                break
                        except Exception:
                            pass
                    await asyncio.sleep(1.0)
                else:
                    return True
            except Exception:
                await asyncio.sleep(0.5)
        return False

    async def dismiss_banners(self, page: Page) -> None:
        """Dismiss cookie consent banners, onboarding dialogs, and promotional popups."""
        dismiss_selectors = [
            # Claude & OneTrust cookie banners
            '#onetrust-accept-btn-handler',
            'button[id*="onetrust-accept"]',
            'button:has-text("Accept all cookies")',
            'button:has-text("Accept All Cookies")',
            'button:has-text("Reject All")',
            'button:has-text("Reject all")',
            'button:has-text("Reject")',
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("Accept cookies")',
            'button:has-text("Accept Cookies")',
            'button:has-text("Accept")',
            'button:has-text("Allow all")',
            'button:has-text("I accept")',
            'button:has-text("I agree")',
            'button:has-text("Acknowledge")',
            'button:has-text("Got it")',
            'button:has-text("I understand")',
            'button[id*="accept"]',
            'button[class*="accept"]',
            'div[class*="cookie"] button',
            'div[id*="cookie"] button',
            # Onboarding & Welcome popups
            'button:has-text("Get started")',
            'button:has-text("Get Started")',
            'button:has-text("Start chatting")',
            'button:has-text("Start Chatting")',
            'button:has-text("Continue without account")',
            'button:has-text("Continue as guest")',
            'button:has-text("Stay logged out")',
            'button:has-text("Dismiss")',
            'button:has-text("Not now")',
            'button:has-text("Maybe later")',
            'button:has-text("Close")',
            'button[aria-label="Close"]',
            'button[aria-label="Dismiss"]',
            'button[data-testid="close-button"]',
        ]
        for sel in dismiss_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    logger.info(f"[{self.display_name}] Auto-dismissing banner/modal: {sel}")
                    await btn.click()
                    await asyncio.sleep(0.3)
            except Exception:
                pass

    async def find_visible_input(
        self,
        page: Page,
        selectors: list[str],
        timeout: float = 15.0,
    ) -> Any | None:
        """Search across selectors and return the first element that is genuinely visible in DOM."""
        logger.info(f"[{self.display_name}] Searching for chat input across {len(selectors)} candidate selectors...")
        start = time.time()
        while time.time() - start < timeout:
            await self.dismiss_banners(page)

            for sel in selectors:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        try:
                            # Skip invisible fallback elements (e.g. ChatGPT wcDTda_fallbackTextarea)
                            class_name = (await el.get_attribute("class") or "").lower()
                            if "fallback" in class_name:
                                continue
                            aria_hidden = await el.get_attribute("aria-hidden")
                            if aria_hidden == "true":
                                continue
                            tabindex = await el.get_attribute("tabindex")
                            if tabindex == "-1":
                                continue
                            if await el.is_visible():
                                tag_name = await el.evaluate("el => el.tagName.toLowerCase()")
                                ph = await el.get_attribute("placeholder") or ""
                                aria = await el.get_attribute("aria-label") or ""
                                logger.info(
                                    f"[{self.display_name}] Matched chat input via '{sel}': "
                                    f"<{tag_name}> placeholder='{ph[:30]}' aria-label='{aria[:30]}' class='{class_name[:35]}'"
                                )
                                return el
                        except Exception:
                            continue
                except Exception:
                    continue
            await asyncio.sleep(0.4)

        try:
            curr_url = page.url
            curr_title = await page.title()
            logger.warning(
                f"[{self.display_name}] Timed out after {timeout}s looking for chat input. "
                f"Page URL: '{curr_url}', Title: '{curr_title}'"
            )
        except Exception:
            pass
        return None

    async def enter_text_safely(self, page: Page, input_el: Any, text: str) -> None:
        """Safely focus, clear, and enter text into textarea or contenteditable div."""
        logger.info(f"[{self.display_name}] Entering prompt ({len(text)} chars) into chat input...")
        try:
            await input_el.focus()
        except Exception:
            pass
        await input_el.click()
        await asyncio.sleep(0.2)
        try:
            tag_name = await input_el.evaluate("el => el.tagName.toLowerCase()")
            is_contenteditable = await input_el.evaluate("el => el.isContentEditable")
            if tag_name in ("textarea", "input") and not is_contenteditable:
                await input_el.fill(text)
                # Dispatch input and change events for reactive frontends
                await input_el.dispatch_event("input")
                await input_el.dispatch_event("change")
            else:
                # Contenteditable rich text (Quill, ProseMirror, Lexical)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.insert_text(text)
                await input_el.dispatch_event("input")
        except Exception as err:
            logger.debug(f"[{self.display_name}] Primary input method failed ({err}), falling back...")
            try:
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.insert_text(text)
            except Exception:
                await input_el.fill(text)
        await asyncio.sleep(0.3)
        logger.info(f"[{self.display_name}] Successfully entered prompt text.")

    async def click_send_or_press_enter(
        self,
        page: Page,
        input_el: Any,
        send_selectors: list[str],
    ) -> None:
        """Find and click send button, or fall back to pressing Enter."""
        send_btn = None
        for sel in send_selectors:
            try:
                elements = await page.query_selector_all(sel)
                for btn in elements:
                    if await btn.is_visible() and not (await btn.is_disabled()):
                        send_btn = btn
                        logger.info(f"[{self.display_name}] Found enabled send button via '{sel}'")
                        break
                if send_btn:
                    break
            except Exception:
                continue

        if send_btn:
            logger.info(f"[{self.display_name}] Clicking send button...")
            await send_btn.click()
        else:
            logger.info(f"[{self.display_name}] No enabled send button found; pressing Enter on input...")
            await input_el.press("Enter")

    @abstractmethod
    async def check_auth(self, context: BrowserContext) -> tuple[bool, str]:
        """Check if user is authenticated. Returns (is_authenticated, details)."""
        pass

    @abstractmethod
    async def generate(
        self,
        context: BrowserContext,
        prompt: str,
        stream: bool = False,
        timeout: int = 120
    ) -> AsyncGenerator[str, None]:
        """Generate response chunks from the browser interface."""
        pass
