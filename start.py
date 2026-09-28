#!/usr/bin/env python
"""
Unified entrypoint for jugaad-llm (Free LLM Token Gateway).

Usage:
  python start.py                         # Normal run (local http://localhost:4000)
  python start.py --login                 # Sign in to ChatGPT, Claude, DeepSeek, Grok (passes bot tests!)
  python start.py --import-profile        # Import cookies/sessions from your real Google Chrome profile
  python start.py --list-profiles         # Show discovered Chrome profiles and logged-in Google accounts
  python start.py --cdp 9222              # Connect directly to your live running Chrome browser
  python start.py --launch-chrome         # Launch regular Chrome with remote debugging port 9222 enabled
  python start.py --tunnel                # Run with public HTTPS ngrok tunnel for hackathons
  python start.py --login --tunnel        # Sign in first, then expose via ngrok tunnel
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import uvicorn

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app.browser.manager import BrowserManager
from app.browser.profile_importer import (
    find_browser_executable,
    find_chrome_user_data_dir,
    get_available_profiles,
    import_chrome_profile,
    is_chrome_running,
    launch_native_chrome_for_login,
    terminate_chrome_processes,
)
from app.config import get_settings
from app.logger import setup_logging
from app.tunnel import NgrokTunnel


def show_profiles_list() -> list[dict[str, any]]:
    """Print discovered Google Chrome profiles with account emails."""
    profiles = get_available_profiles()
    user_data = find_chrome_user_data_dir()
    print("\n" + "=" * 70)
    print("🔍 DISCOVERED GOOGLE CHROME PROFILES")
    print(f"Chrome User Data location: {user_data}")
    print("=" * 70)

    if not profiles:
        print("  No existing Chrome profiles found at default location.")
        return []

    for idx, p in enumerate(profiles, start=1):
        email_str = f" <{p['email']}>" if p['email'] else " (No email detected)"
        gaia_str = f" - {p['gaia_name']}" if p.get('gaia_name') else ""
        star = " ★ [RECOMMENDED]" if idx == 1 and p.get('is_signed_in') else ""
        print(f"  [{idx}] {p['id']}: {p['name']}{email_str}{gaia_str}{star}")
    print("=" * 70 + "\n")
    return profiles


def handle_profile_import(profile_arg: str | None, target_user_data: Path) -> bool:
    """Import Chrome profile into gateway data directory."""
    profiles = get_available_profiles()
    if not profiles:
        print("⚠️ No Chrome profiles found to import.")
        return False

    chosen_id = "Default"
    if profile_arg and profile_arg.strip():
        # Match by ID, email, or number
        arg = profile_arg.strip().lower()
        matched = None
        if arg.isdigit() and 1 <= int(arg) <= len(profiles):
            matched = profiles[int(arg) - 1]["id"]
        else:
            for p in profiles:
                if p["id"].lower() == arg or p["email"].lower() == arg:
                    matched = p["id"]
                    break
        if matched:
            chosen_id = matched
        else:
            print(f"⚠️ Profile '{profile_arg}' not found, falling back to '{chosen_id}'")

    print(f"\n📦 Importing Chrome profile '{chosen_id}' into {target_user_data.resolve()}...")
    if is_chrome_running():
        print("⚠️ Chrome background processes are running (holding file locks on session cookies).")
        print("Closing background Chrome processes to release locks...")
        terminate_chrome_processes()
        time.sleep(1.0)

    ok, msg = import_chrome_profile(
        target_user_data=target_user_data,
        profile_id=chosen_id,
        force_close_chrome=True,
    )
    if ok:
        print(f"✅ {msg}")
        print("🎉 Your Google login session and cookies have been imported successfully!\n")
        return True
    else:
        print(f"⚠️ Import note: {msg}\n")
        return False


def run_interactive_login(user_data_dir: Path, auto_import: bool = False) -> None:
    """Launch native un-automated Chrome for interactive sign-in without bot blocks."""
    print("=" * 70)
    print("🔐 BROWSER AUTHENTICATION SETUP")
    print(f"Profile location: {user_data_dir.resolve()}")
    print("=" * 70)

    profiles = get_available_profiles()
    if profiles:
        print("Detected installed Google Chrome profile(s) with logged-in accounts:")
        for idx, p in enumerate(profiles[:4], start=1):
            email_info = f" ({p['email']})" if p.get("email") else ""
            print(f"  [{idx}] {p['id']}: {p['name']}{email_info}")

        # Check if already imported
        already_has_cookies = (user_data_dir / "Default" / "Network" / "Cookies").exists()

        if not already_has_cookies or auto_import:
            print("\n💡 TIP: We can copy your existing Google account session directly")
            print("   so you don't even have to type your password or 2FA!")
            choice = input("👉 Import your logged-in Google account from profile [1]? [Y/n] > ").strip().lower()
            if choice not in ("n", "no"):
                selected_profile = profiles[0]["id"]
                handle_profile_import(selected_profile, user_data_dir)
        else:
            print("\n💡 An existing profile is already present in your gateway data folder.")
            reimport = input("👉 Re-import / refresh from your standard Chrome profile? [y/N] > ").strip().lower()
            if reimport in ("y", "yes"):
                selected_profile = profiles[0]["id"]
                handle_profile_import(selected_profile, user_data_dir)

    print("\n🌐 Launching native Google Chrome for interactive login...")
    print("✨ IMPORTANT:")
    print("   • This runs NATIVE Chrome (no Playwright automation hooks).")
    print("   • Cloudflare Turnstile bot tests will PASS normally.")
    print("   • Google OAuth ('Continue with Google') will NOT block you with 'browser not secure'.")
    print("=" * 70)

    urls = [
        "https://chatgpt.com",
        "https://claude.ai",
        "https://chat.deepseek.com",
        "https://grok.com",
        "https://gemini.google.com/app",
        "https://www.perplexity.ai",
        "https://chat.mistral.ai/chat",
        "https://huggingface.co/chat",
        "https://poe.com",
        "https://k2think.ai",
    ]

    chrome_proc = None
    try:
        chrome_proc = launch_native_chrome_for_login(user_data_dir=user_data_dir, urls=urls)
    except Exception as e:
        print(f"⚠️ Could not launch native Chrome directly: {e}")
        print("Falling back to Playwright persistent context with anti-bot stealth...")
        from playwright.sync_api import sync_playwright
        from app.browser.manager import STEALTH_JS

        user_data_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            channels = ["chrome", "msedge", None]
            context = None
            for ch in channels:
                try:
                    kwargs = {
                        "user_data_dir": str(user_data_dir),
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
                    context = p.chromium.launch_persistent_context(**kwargs)
                    context.add_init_script(STEALTH_JS)
                    break
                except Exception:
                    continue

            if context:
                for url in urls:
                    try:
                        page = context.new_page()
                        page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    except Exception:
                        pass
                print("\n👉 Please sign into your free accounts in the open tabs.")
                input("\n[PRESS ENTER TO FINISH LOGIN] > ")
                context.close()
                print("✅ Browser authentication saved! Starting LLM Gateway...\n")
                return

    print("\n👉 Tabs for ChatGPT, Claude, DeepSeek, and Grok have opened.")
    print("👉 If you imported your profile, click 'Continue with Google' to sign in with 1 click!")
    print("👉 Once you've logged in, return here and press [ENTER] to save sessions.")

    try:
        input("\n[PRESS ENTER TO FINISH LOGIN] > ")
    except (KeyboardInterrupt, EOFError):
        pass

    if chrome_proc:
        print("Closing setup browser window to release locks for gateway service...")
        try:
            chrome_proc.terminate()
            chrome_proc.wait(timeout=3)
        except Exception:
            pass
        # Ensure lingering processes are terminated so SQLite lock is freed
        time.sleep(1.0)

    print("✅ Browser authentication saved! Starting LLM Gateway...\n")


def print_banner(local_url: str, public_url: str | None = None, master_key: str | None = None, cdp_mode: str | None = None):
    api_key_str = master_key or "sk-hackathon-token"
    print("\n" + "═" * 72)
    print(" 🧙‍♂️ JUGAAD-LLM: FREE LLM TOKEN GATEWAY IS LIVE!")
    print("─" * 72)
    print(f" 🏠 Landing Page:             {local_url}/")
    print(f" 🎛️  Dashboard & Test Console: {local_url}/dashboard")
    print(f" 🔗 Local OpenAI Base URL:    {local_url}/v1")
    if public_url:
        print(f" 🌍 Public Ngrok Base URL:   {public_url}/v1")
    if cdp_mode:
        print(f" 🔌 CDP Browser Attached:    {cdp_mode}")
    print("─" * 72)
    print(" 📋 COPY INTO YOUR HACKATHON .env:")
    if public_url:
        print(f"    OPENAI_BASE_URL=\"{public_url}/v1\"")
    else:
        print(f"    OPENAI_BASE_URL=\"{local_url}/v1\"")
    print(f"    OPENAI_API_KEY=\"{api_key_str}\"")
    print("\n 🤖 SUPPORTED MODELS IN YOUR CODE / AGENTS:")
    print("    • 'auto'                     -> Smart load-balanced default (least loaded active)")
    print("    • 'chatgpt' or 'gpt-4o'      -> ChatGPT Web Free Session")
    print("    • 'claude' or 'claude-3-5'   -> Claude.ai Web Free Session")
    print("    • 'deepseek' or 'deepseek-r1'-> DeepSeek Web Free Session")
    print("    • 'grok' or 'grok-3'         -> Grok Web Free Session")
    print("    • 'gemini' or 'gemini-1.5'   -> Google Gemini Web Free Session")
    print("    • 'perplexity' or 'sonar'    -> Perplexity Search/Chat Free Session")
    print("    • 'mistral' or 'lechat'      -> Mistral Le Chat Free Session")
    print("    • 'huggingface' or 'hf'      -> HuggingFace Chat Free Session")
    print("    • 'poe' or 'poe-chat'        -> Poe Free Tier Session")
    print("    • 'k2think' or 'k2'          -> K2 Think Reasoning Free Session")
    print("    • 'ollama' or 'llama3'       -> Ollama Cloud / Local Instance")
    print("═" * 72 + "\n")


async def run_validation(user_data_dir: Path, cdp_url: str | None = None) -> None:
    """Validate all browser providers and print their status."""
    print("\n" + "=" * 80)
    print("🔍 VALIDATING BROWSER OPENROUTER SESSIONS & CHAT INPUTS")
    print("=" * 80)
    manager = BrowserManager(user_data_dir=user_data_dir, headless=True, cdp_url=cdp_url)
    try:
        await manager.initialize()
        results = await manager.check_all_providers()
        print(f"\n{'PROVIDER':<15} {'STATUS':<18} {'DETAILS'}")
        print("-" * 80)
        for name, data in results.items():
            auth = data.get("authenticated", False)
            status_label = "✅ READY" if auth else "⚠️  SIGN-IN NEEDED"
            msg = data.get("message", "")
            print(f"{name:<15} {status_label:<18} {msg}")
        print("=" * 80)
        ready_count = sum(1 for d in results.values() if d.get("authenticated"))
        print(f"Summary: {ready_count}/{len(results)} providers authenticated and ready.")
        print("Run 'python start.py --login' to sign in to any remaining services.\n")
    finally:
        await manager.close()


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="jugaad-llm: Free LLM Token Gateway")
    parser.add_argument("--validate", action="store_true", help="Validate authentication and chat input readiness across all model providers")
    parser.add_argument("--login", action="store_true", help="Launch native Chrome window to sign into accounts")
    parser.add_argument("--import-profile", nargs="?", const="Default", default=None, help="Import Google account and sessions from a Chrome profile (e.g. Default or Profile 20)")
    parser.add_argument("--list-profiles", action="store_true", help="List discovered Chrome profiles and associated Google emails")
    parser.add_argument("--cdp", nargs="?", const="http://localhost:9222", default=None, help="Connect to an existing Chrome browser with remote debugging (e.g. 9222 or http://localhost:9222)")
    parser.add_argument("--launch-chrome", action="store_true", help="Launch regular Google Chrome with remote debugging port 9222 enabled")
    parser.add_argument("--tunnel", action="store_true", help="Expose temporary HTTPS ngrok tunnel")
    parser.add_argument("--headless", action="store_true", help="Run browser in background headless mode")
    parser.add_argument("--port", type=int, default=None, help="Port to run gateway on (default: from .env or 4000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    args = parser.parse_args()

    settings = get_settings()
    port = args.port or settings.litellm_port
    user_data_dir = Path(settings.chrome_user_data_dir)

    # Sub-command: List profiles and exit
    if args.list_profiles:
        show_profiles_list()
        sys.exit(0)

    # Sub-command: Validate all provider sessions and chat inputs
    if args.validate:
        asyncio.run(run_validation(user_data_dir, cdp_url=args.cdp))
        sys.exit(0)

    # Sub-command: Launch regular Chrome with CDP port enabled
    if args.launch_chrome:
        chrome_exe = find_browser_executable("chrome")
        if not chrome_exe:
            print("❌ Google Chrome executable not found.")
            sys.exit(1)
        cdp_port = 9222
        print(f"🚀 Launching Google Chrome with remote debugging port {cdp_port}...")
        cmd = [chrome_exe, f"--remote-debugging-port={cdp_port}"]
        subprocess.Popen(cmd)
        print(f"✅ Chrome launched with debugging on port {cdp_port}.")
        print(f"Now you can run: python start.py --cdp {cdp_port}")
        sys.exit(0)

    # Sub-command: Direct profile import
    if args.import_profile is not None:
        handle_profile_import(args.import_profile, user_data_dir)
        if not args.login:
            print("To verify or sign into providers now, run: python start.py --login")

    # Step 1: Run login if requested
    if args.login:
        run_interactive_login(user_data_dir)

    # Step 2: Configure CDP if passed
    cdp_url = None
    if args.cdp:
        cdp_val = args.cdp
        if cdp_val.isdigit():
            cdp_url = f"http://localhost:{cdp_val}"
        elif not cdp_val.startswith("http"):
            cdp_url = f"http://{cdp_val}"
        else:
            cdp_url = cdp_val
        os.environ["BROWSER_CDP_URL"] = cdp_url
        print(f"🔌 Configured CDP connection to: {cdp_url}")

    if args.headless:
        os.environ["BROWSER_HEADLESS"] = "true"

    # Step 3: Establish tunnel if requested
    tunnel = None
    public_url = None
    if args.tunnel:
        print("Starting ngrok tunnel for hackathon exposure...")
        try:
            tunnel = NgrokTunnel(port=port, authtoken=settings.ngrok_authtoken)
            public_url = tunnel.start()
        except Exception as e:
            print(f"⚠️ Ngrok tunnel failed to start: {e}")
            print("Running in local mode only.")

    local_url = f"http://localhost:{port}"
    print_banner(
        local_url=local_url,
        public_url=public_url,
        master_key=settings.litellm_master_key,
        cdp_mode=cdp_url,
    )

    # Step 4: Run server
    from app.main import app
    if public_url:
        app.state.tunnel_url = public_url

    try:
        uvicorn.run("app.main:app", host=args.host, port=port, reload=False)
    finally:
        if tunnel:
            tunnel.stop()


if __name__ == "__main__":
    main()
