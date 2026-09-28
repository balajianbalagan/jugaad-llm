from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from playwright.async_api import BrowserContext, Playwright, async_playwright

from app.browser.profile_importer import (
    launch_native_chrome_for_login,
    terminate_chrome_processes,
)
from app.browser.providers.base import (
    AuthenticationRequiredError,
    BaseBrowserProvider,
    ProviderRateLimitError,
    format_messages,
)
from app.browser.providers.chatgpt import ChatGPTProvider
from app.browser.providers.claude import ClaudeProvider
from app.browser.providers.deepseek import DeepSeekProvider
from app.browser.providers.gemini import GeminiProvider
from app.browser.providers.grok import GrokProvider
from app.browser.providers.huggingface import HuggingFaceProvider
from app.browser.providers.k2think import K2ThinkProvider
from app.browser.providers.mistral import MistralProvider
from app.browser.providers.ollama import OllamaProvider
from app.browser.providers.perplexity import PerplexityProvider
from app.browser.providers.poe import PoeProvider

logger = logging.getLogger(__name__)

STEALTH_JS = """
// 1. Mask navigator.webdriver
try {
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true,
    });
} catch (e) {}

// 2. Emulate window.chrome
try {
    if (!window.chrome) {
        window.chrome = {};
    }
    if (!window.chrome.runtime) {
        window.chrome.runtime = {
            PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
            PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
            PlatformNaclArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
            onMessage: { addListener: function() {}, removeListener: function() {}, hasListener: function() {} },
        };
    }
} catch (e) {}

// 3. Emulate plugins
try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
            configurable: true,
        });
    }
} catch (e) {}

// 4. Emulate permissions query
try {
    const originalQuery = window.navigator.permissions ? window.navigator.permissions.query : null;
    if (originalQuery) {
        window.navigator.permissions.query = (parameters) => (
            parameters && parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
    }
} catch (e) {}

// 5. Emulate languages
try {
    if (!navigator.languages || navigator.languages.length === 0) {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
            configurable: true,
        });
    }
} catch (e) {}

// 6. Mask headless user-agent signature
try {
    if (navigator.userAgent.includes("Headless")) {
        const cleanUa = navigator.userAgent.replace(/HeadlessChrome/g, 'Chrome').replace(/Headless/g, '');
        Object.defineProperty(navigator, 'userAgent', {
            get: () => cleanUa,
            configurable: true,
        });
    }
} catch (e) {}

// 7. Emulate screen properties
try {
    if (!window.screen || window.screen.colorDepth === 0) {
        Object.defineProperty(window.screen, 'colorDepth', { get: () => 24, configurable: true });
    }
} catch (e) {}
"""


class BrowserManager:
    """Manages the persistent Chromium context and orchestrates browser LLM providers."""

    def __init__(
        self,
        user_data_dir: str | Path = "data/chrome_profile",
        headless: bool = False,
        cdp_url: str | None = None,
    ):
        self.user_data_dir = Path(user_data_dir)
        self.headless = headless
        self.cdp_url = cdp_url

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._lock = asyncio.Lock()

        # Register providers
        self.providers: dict[str, BaseBrowserProvider] = {
            "chatgpt": ChatGPTProvider(),
            "claude": ClaudeProvider(),
            "deepseek": DeepSeekProvider(),
            "grok": GrokProvider(),
            "gemini": GeminiProvider(),
            "perplexity": PerplexityProvider(),
            "mistral": MistralProvider(),
            "huggingface": HuggingFaceProvider(),
            "poe": PoeProvider(),
            "k2think": K2ThinkProvider(),
            "ollama": OllamaProvider(),
        }

        # Build alias lookup map
        self._model_map: dict[str, BaseBrowserProvider] = {}
        for p in self.providers.values():
            self._model_map[p.name.lower()] = p
            for alias in p.model_aliases:
                self._model_map[alias.lower()] = p

        # Load balancing and authentication tracking
        self._in_flight: dict[str, int] = {k: 0 for k in self.providers}
        self._last_used: dict[str, float] = {k: 0.0 for k in self.providers}
        self._auth_cache: dict[str, tuple[bool, float, str]] = {}
        self._cooldown_until: dict[str, float] = {}
        self._login_proc: Any = None
        self._interactive_context: BrowserContext | None = None

    @property
    def supported_models(self) -> list[dict[str, Any]]:
        """Return OpenAI-compatible models list."""
        models = []
        for p in self.providers.values():
            for alias in p.model_aliases:
                models.append({
                    "id": alias,
                    "object": "model",
                    "owned_by": f"browser/{p.name}",
                    "permission": [],
                    "root": p.name,
                    "parent": None,
                })
        # Add 'auto' meta-model
        models.append({
            "id": "auto",
            "object": "model",
            "owned_by": "browser/auto",
            "permission": [],
            "root": "auto",
            "parent": None,
        })
        return models

    async def initialize(self) -> None:
        """Start Playwright and launch or connect to persistent browser context."""
        async with self._lock:
            if self._context:
                return

            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()

            # Attempt CDP connection if configured or open
            if self.cdp_url:
                try:
                    logger.info(f"Connecting to existing browser via CDP: {self.cdp_url}")
                    browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
                    if browser.contexts:
                        self._context = browser.contexts[0]
                    else:
                        self._context = await browser.new_context()
                    try:
                        await self._context.add_init_script(STEALTH_JS)
                    except Exception:
                        pass
                    logger.info("Successfully connected to CDP browser session.")
                    return
                except Exception as e:
                    logger.warning(f"Could not connect to CDP ({e}); falling back to persistent profile.")

            # Launch persistent context using system Google Chrome with high-performance flags
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
                "--disable-infobars",
                "--disable-features=IsolateOrigins,site-per-process,Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider",
                "--disable-search-engine-choice-screen",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-ipc-flooding-protection",
                "--disable-dev-shm-usage",
                "--disable-breakpad",
                "--disable-component-update",
                "--password-store=basic",
                "--window-size=1280,800",
            ]
            if self.headless:
                launch_args.extend([
                    "--headless=new",
                    "--disable-gpu",
                    "--mute-audio",
                    "--hide-scrollbars",
                ])

            # Try Chrome first, then Edge, then default Chromium
            channels = ["chrome", "msedge", None]
            context = None
            last_err = None

            for ch in channels:
                try:
                    logger.info(f"Launching persistent browser context (channel={ch}, dir={self.user_data_dir}, headless={self.headless})...")
                    kwargs: dict[str, Any] = {
                        "user_data_dir": str(self.user_data_dir),
                        "headless": self.headless,
                        "args": launch_args,
                        "ignore_default_args": ["--enable-automation"],
                        "viewport": {"width": 1280, "height": 800},
                        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    }
                    if ch:
                        kwargs["channel"] = ch
                    context = await self._playwright.chromium.launch_persistent_context(**kwargs)
                    logger.info(f"Browser launched successfully with channel: {ch or 'chromium'}")
                    break
                except Exception as err:
                    last_err = err
                    logger.debug(f"Channel {ch} failed: {err}")

            if not context:
                raise RuntimeError(f"Failed to launch browser with any channel: {last_err}")

            try:
                await context.add_init_script(STEALTH_JS)
            except Exception:
                pass

            # Abort heavy trackers and analytics to dramatically accelerate page loads
            async def _block_unneeded_resources(route):
                try:
                    url = route.request.url.lower()
                    blocked_patterns = (
                        "google-analytics.com",
                        "googletagmanager.com",
                        "doubleclick.net",
                        "sentry.io",
                        "browser.sentry-cdn.com",
                        "statsig.com",
                        "amplitude.com",
                        "segment.io",
                        "hotjar.com",
                        "clarity.ms",
                        "datadoghq.com",
                        "intercom.io",
                    )
                    if any(p in url for p in blocked_patterns):
                        await route.abort()
                    elif route.request.resource_type in ("media", "beacon"):
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            try:
                await context.route("**/*", _block_unneeded_resources)
            except Exception:
                pass

            self._context = context

    async def close(self) -> None:
        """Clean up browser context and Playwright instance."""
        async with self._lock:
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    async def warmup(self) -> None:
        """Warm up browser context and pre-load the default provider tab in the background."""
        await self.initialize()
        if not self._context:
            return
        try:
            # Pre-warm candidate providers if tabs are blank
            primary_providers = [
                self.providers.get("chatgpt"),
                self.providers.get("claude"),
                self.providers.get("deepseek"),
                self.providers.get("gemini"),
            ]
            for p in primary_providers:
                if p:
                    page = await p._get_page(self._context)
                    if page.url in ("about:blank", "chrome://newtab/"):
                        logger.info(f"[Warmup] Pre-navigating tab for '{p.name}' ({p.home_url})...")
                        await page.goto(p.home_url, wait_until="domcontentloaded", timeout=20000)
                    break
        except Exception as e:
            logger.debug(f"[Warmup] Non-critical background warmup note: {e}")

    def get_provider_for_model(self, model_name: str) -> BaseBrowserProvider:
        """Synchronously resolve a model name or alias to a browser provider."""
        key = model_name.strip().lower()
        now = time.time()

        if key in ("auto", "default", "free", "fastest"):
            # Select candidate with least in-flight and not cooling down
            candidates = [
                p for name, p in self.providers.items()
                if self._cooldown_until.get(name, 0) <= now
                and (self._auth_cache.get(name) is None or self._auth_cache[name][0] is True)
            ]
            if not candidates:
                candidates = list(self.providers.values())
            candidates.sort(key=lambda p: (self._in_flight.get(p.name, 0), self._last_used.get(p.name, 0)))
            return candidates[0]

        provider = self._model_map.get(key)
        if not provider:
            for name, prov in self.providers.items():
                if name in key:
                    return prov
            raise ValueError(
                f"Unknown model '{model_name}'. Available browser models: "
                f"{list(self.providers.keys())} or aliases like {list(self._model_map.keys())[:10]}"
            )
        return provider

    async def select_and_validate_provider(self, model_name: str) -> BaseBrowserProvider:
        """Intelligently route request, verify authentication, and balance load across services."""
        key = model_name.strip().lower()
        now = time.time()

        if key in ("auto", "default", "free", "fastest"):
            # 1. Filter candidates that are authenticated and not cooling down
            candidates: list[BaseBrowserProvider] = []
            for name, prov in self.providers.items():
                if self._cooldown_until.get(name, 0) > now:
                    continue

                cached = self._auth_cache.get(name)
                if cached is not None:
                    is_auth, ts, _ = cached
                    if is_auth and (now - ts < 600):
                        candidates.append(prov)
                    continue

                # If not cached, verify candidate if browser context is running
                if self._context:
                    try:
                        is_auth, msg = await prov.check_auth(self._context)
                        self._auth_cache[name] = (is_auth, now, msg)
                        if is_auth:
                            candidates.append(prov)
                    except Exception:
                        self._auth_cache[name] = (False, now, "Check failed")

            if not candidates:
                raise AuthenticationRequiredError(
                    "No browser providers are currently logged in. "
                    "Run 'python start.py --login' to sign in to ChatGPT, Claude, Gemini, DeepSeek, Mistral, Perplexity, Poe, or K2 Think."
                )

            # 2. Least-loaded + round-robin distribution to prevent overloading any single service
            candidates.sort(key=lambda p: (self._in_flight.get(p.name, 0), self._last_used.get(p.name, 0)))
            chosen = candidates[0]
            logger.info(
                f"[Router] Auto-selected provider '{chosen.name}' "
                f"(in-flight: {self._in_flight.get(chosen.name, 0)}, last used: {int(now - self._last_used.get(chosen.name, 0))}s ago)"
            )
            return chosen

        # Specific model requested
        provider = self.get_provider_for_model(model_name)

        # Pre-check login status: "if its not logged in we better not try it"
        cached = self._auth_cache.get(provider.name)
        if cached is not None:
            is_auth, ts, msg = cached
            if not is_auth and now - ts < 600:
                raise AuthenticationRequiredError(
                    f"[{provider.display_name}] Session is not logged in ({msg}). "
                    f"Please sign in with 'python start.py --login' or via dashboard before trying this model."
                )
        elif self._context:
            try:
                is_auth, msg = await provider.check_auth(self._context)
                self._auth_cache[provider.name] = (is_auth, now, msg)
                if not is_auth:
                    raise AuthenticationRequiredError(
                        f"[{provider.display_name}] Session is not logged in ({msg}). "
                        f"Please sign in with 'python start.py --login' before trying this model."
                    )
            except AuthenticationRequiredError:
                raise
            except Exception as e:
                logger.warning(f"Could not verify auth for {provider.name}: {e}")

        return provider

    async def check_all_providers(self) -> dict[str, dict[str, Any]]:
        """Check authentication and access status of all browser providers."""
        if not self._context:
            await self.initialize()

        results = {}
        for name, provider in self.providers.items():
            logger.info(f"[Validation] Checking status for provider '{name}' ({provider.display_name})...")
            try:
                is_auth, msg = await asyncio.wait_for(provider.check_auth(self._context), timeout=15.0)
                self._auth_cache[name] = (is_auth, time.time(), msg)
                logger.info(f"[Validation] '{name}' status: authenticated={is_auth}, details='{msg}'")
                results[name] = {
                    "provider": name,
                    "display_name": provider.display_name,
                    "status": "ready" if is_auth else "needs_login",
                    "authenticated": is_auth,
                    "message": msg,
                    "models": provider.model_aliases,
                }
            except TimeoutError:
                msg = "Validation timed out after 15s"
                logger.warning(f"[Validation] '{name}' check timed out.")
                self._auth_cache[name] = (False, time.time(), msg)
                results[name] = {
                    "provider": name,
                    "display_name": provider.display_name,
                    "status": "timeout",
                    "authenticated": False,
                    "message": msg,
                    "models": provider.model_aliases,
                }
            except Exception as e:
                logger.warning(f"[Validation] '{name}' check encountered error: {e}")
                self._auth_cache[name] = (False, time.time(), str(e))
                results[name] = {
                    "provider": name,
                    "display_name": provider.display_name,
                    "status": "error",
                    "authenticated": False,
                    "message": str(e),
                    "models": provider.model_aliases,
                }
        return results

    async def launch_interactive_login(self, provider_name: str | None = None) -> str:
        """Open a native un-automated Chrome window for signing into accounts without bot blocks."""
        logger.info(f"Opening interactive login browser for: {provider_name or 'all'}...")
        if self._context:
            await self.close()

        provider_urls = {
            "chatgpt": "https://chatgpt.com",
            "claude": "https://claude.ai/login",
            "deepseek": "https://chat.deepseek.com",
            "grok": "https://grok.com",
            "gemini": "https://gemini.google.com/app",
            "perplexity": "https://www.perplexity.ai",
            "mistral": "https://chat.mistral.ai/chat",
            "huggingface": "https://huggingface.co/chat",
            "poe": "https://poe.com/login",
            "k2think": "https://chat.ifm.ai/",
            "ollama": "http://localhost:11434",
        }

        if provider_name and provider_name.lower() in provider_urls:
            urls = [provider_urls[provider_name.lower()]]
            target_desc = f"{provider_name} ({urls[0]})"
        else:
            urls = list(provider_urls.values())
            target_desc = "all providers"

        # Terminate any existing login process before starting a new one
        if self._interactive_context:
            try:
                await self._interactive_context.close()
            except Exception:
                pass
            self._interactive_context = None

        if self._login_proc:
            terminate_chrome_processes(user_data_dir=self.user_data_dir, proc=self._login_proc)
            self._login_proc = None

        # Prefer native Chrome launch (100% free of automation hooks, passes Google and Cloudflare tests)
        try:
            self._login_proc = launch_native_chrome_for_login(user_data_dir=self.user_data_dir, urls=urls)
            logger.info(f"Native Chrome window opened for {target_desc} without automation hooks. User can sign in freely.")
            return urls[0] if provider_name else "all"
        except Exception as native_err:
            logger.warning(f"Could not launch native Chrome directly ({native_err}). Falling back to Playwright with stealth.")

        playwright = await async_playwright().start()
        channels = ["chrome", "msedge", None]
        context = None
        for ch in channels:
            try:
                kwargs: dict[str, Any] = {
                    "user_data_dir": str(self.user_data_dir),
                    "headless": False,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-default-browser-check",
                        "--no-first-run",
                        "--disable-infobars",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--disable-search-engine-choice-screen",
                    ],
                    "ignore_default_args": ["--enable-automation"],
                    "no_viewport": True,
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                }
                if ch:
                    kwargs["channel"] = ch
                context = await playwright.chromium.launch_persistent_context(**kwargs)
                await context.add_init_script(STEALTH_JS)
                break
            except Exception:
                continue

        if not context:
            raise RuntimeError("Could not launch browser for interactive login.")

        self._interactive_context = context

        for i, url in enumerate(urls):
            if i == 0 and context.pages:
                p = context.pages[0]
            else:
                p = await context.new_page()
            try:
                await p.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

        logger.info(f"Tabs opened with stealth for {target_desc}. Waiting for user interaction.")
        return urls[0] if provider_name else "all"

    async def save_session_and_reload(self, provider_name: str | None = None) -> dict[str, Any]:
        """Terminate the interactive login Chrome process, flush locks, and reload/validate sessions."""
        logger.info(f"Saving sessions and re-initializing browser context (target: {provider_name or 'all'})...")
        
        # 1. Close fallback interactive Playwright context if one was open
        if self._interactive_context:
            try:
                await self._interactive_context.close()
            except Exception:
                pass
            self._interactive_context = None

        # 2. Terminate the native login Chrome process tree and any processes locking self.user_data_dir
        # Crucially: This NEVER closes the user's personal browser or active work!
        terminate_chrome_processes(user_data_dir=self.user_data_dir, proc=self._login_proc)
        self._login_proc = None

        await asyncio.sleep(0.8)

        # Clear cached authentication
        if provider_name:
            self._auth_cache.pop(provider_name, None)
        else:
            self._auth_cache.clear()

        # Re-initialize browser context
        if self._context:
            await self.close()
        await self.initialize()

        # Check authentication
        if provider_name and provider_name in self.providers:
            prov = self.providers[provider_name]
            is_auth, msg = await prov.check_auth(self._context)
            self._auth_cache[provider_name] = (is_auth, time.time(), msg)
            return {
                "provider": provider_name,
                "authenticated": is_auth,
                "message": msg,
                "status": "ready" if is_auth else "needs_login",
            }
        else:
            return await self.check_all_providers()

    async def refresh_provider(self, provider_name: str) -> dict[str, Any]:
        """Invalidate auth cache for a single provider and re-check its live session."""
        if not self._context:
            await self.initialize()

        if provider_name not in self.providers:
            raise ValueError(f"Unknown provider '{provider_name}'")

        self._auth_cache.pop(provider_name, None)
        prov = self.providers[provider_name]
        is_auth, msg = await prov.check_auth(self._context)
        self._auth_cache[provider_name] = (is_auth, time.time(), msg)
        return {
            "provider": provider_name,
            "authenticated": is_auth,
            "message": msg,
            "status": "ready" if is_auth else "needs_login",
        }

    async def complete_stream(
        self,
        model: str,
        messages: list[Any],
        timeout: int = 120,
    ) -> AsyncGenerator[str, None]:
        """Generate streaming completion with overload prevention and login validation."""
        if not self._context:
            await self.initialize()

        provider = await self.select_and_validate_provider(model)
        prompt = format_messages(messages)

        # Increment in-flight counter to prevent service overload
        self._in_flight[provider.name] = self._in_flight.get(provider.name, 0) + 1
        self._last_used[provider.name] = time.time()

        try:
            async for chunk in provider.generate(self._context, prompt, stream=True, timeout=timeout):
                yield chunk
            # Mark provider as verified healthy
            self._auth_cache[provider.name] = (True, time.time(), "Active")
        except AuthenticationRequiredError as auth_err:
            self._auth_cache[provider.name] = (False, time.time(), str(auth_err))
            raise AuthenticationRequiredError(
                f"[{provider.display_name}] Session not authenticated. "
                f"Run 'python start.py --login' to sign in to your free account."
            ) from auth_err
        except ProviderRateLimitError as rate_err:
            # 60s cooldown so auto-router protects this provider from being overloaded
            self._cooldown_until[provider.name] = time.time() + 60.0
            raise ProviderRateLimitError(
                f"[{provider.display_name}] Free tier limit reached: {rate_err}. "
                f"Auto-router will temporarily avoid overloading this provider."
            ) from rate_err
        finally:
            self._in_flight[provider.name] = max(0, self._in_flight.get(provider.name, 1) - 1)
