from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Known Chrome installation paths across OS
CHROME_SEARCH_PATHS = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]

EDGE_SEARCH_PATHS = [
    # Windows
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]


def find_browser_executable(preferred: str = "chrome") -> str | None:
    """Find installed Chrome or Edge executable on the host system."""
    paths_to_check = CHROME_SEARCH_PATHS if preferred == "chrome" else EDGE_SEARCH_PATHS
    fallback_paths = EDGE_SEARCH_PATHS if preferred == "chrome" else CHROME_SEARCH_PATHS

    for path_str in paths_to_check:
        if path_str and Path(path_str).is_file():
            return path_str

    for path_str in fallback_paths:
        if path_str and Path(path_str).is_file():
            return path_str

    # Check PATH
    cmd = "chrome" if preferred == "chrome" else "msedge"
    found = shutil.which(cmd)
    if found:
        return found
    return shutil.which("msedge" if preferred == "chrome" else "chrome")


def find_chrome_user_data_dir() -> Path | None:
    """Locate the default system Google Chrome User Data directory."""
    system = platform.system()
    if system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            path = Path(local_app_data) / "Google" / "Chrome" / "User Data"
            if path.exists():
                return path
    elif system == "Darwin":
        path = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        if path.exists():
            return path
    elif system == "Linux":
        path = Path.home() / ".config" / "google-chrome"
        if path.exists():
            return path
    return None


def get_available_profiles(chrome_user_data: Path | None = None) -> list[dict[str, Any]]:
    """List all user profiles found in the system Chrome directory with account info."""
    user_data = chrome_user_data or find_chrome_user_data_dir()
    if not user_data or not user_data.exists():
        return []

    local_state_file = user_data / "Local State"
    profiles: list[dict[str, Any]] = []

    if local_state_file.exists():
        try:
            with open(local_state_file, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            info_cache = state_data.get("profile", {}).get("info_cache", {})
            for profile_id, info in info_cache.items():
                profile_dir = user_data / profile_id
                if profile_dir.exists():
                    profiles.append({
                        "id": profile_id,
                        "name": info.get("name", profile_id),
                        "email": info.get("user_name", "") or "",
                        "gaia_name": info.get("gaia_name", "") or "",
                        "is_signed_in": bool(info.get("user_name")),
                        "path": str(profile_dir),
                    })
        except Exception as e:
            logger.warning(f"Could not parse Chrome Local State: {e}")

    # Fallback to checking folders directly if info_cache is empty
    if not profiles:
        for entry in user_data.iterdir():
            if entry.is_dir() and (entry.name == "Default" or entry.name.startswith("Profile ")):
                profiles.append({
                    "id": entry.name,
                    "name": entry.name,
                    "email": "",
                    "gaia_name": "",
                    "is_signed_in": False,
                    "path": str(entry),
                })

    # Sort profiles: signed-in accounts first, then Default, then others
    def sort_key(p: dict[str, Any]):
        return (
            0 if p["email"] else 1,
            0 if p["id"] == "Default" else 1,
            p["id"],
        )

    profiles.sort(key=sort_key)
    return profiles


def is_chrome_running() -> bool:
    """Check if any Google Chrome process is currently running."""
    if platform.system() == "Windows":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return "chrome.exe" in out.lower()
        except Exception:
            return False
    else:
        try:
            out = subprocess.check_output(["pgrep", "-f", "chrome"], text=True)
            return bool(out.strip())
        except Exception:
            return False


def terminate_chrome_processes() -> bool:
    """Terminate running Chrome background/foreground processes to release file locks."""
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            time.sleep(1.0)
            return not is_chrome_running()
        except Exception as e:
            logger.warning(f"Failed to terminate Chrome processes: {e}")
            return False
    else:
        try:
            subprocess.run(["pkill", "-9", "-f", "chrome"], check=False)
            time.sleep(1.0)
            return not is_chrome_running()
        except Exception as e:
            logger.warning(f"Failed to kill chrome: {e}")
            return False


# Essential files and subdirectories needed for session cookies, accounts, and local state
AUTH_FILES = [
    "Preferences",
    "Secure Preferences",
    "Web Data",
    "Web Data-journal",
    "Login Data",
    "Login Data-journal",
    "Login Data For Account",
    "Login Data For Account-journal",
    "Account Web Data",
    "Account Web Data-journal",
]

AUTH_DIRS = [
    "Network",  # Contains Cookies, Cookies-journal, Network Persistent State
    "Local Storage",
    "Session Storage",
    "IndexedDB",
    "Sync Data",
    "Storage",
]


def import_chrome_profile(
    source_user_data: Path | None = None,
    target_user_data: Path | str = "data/chrome_profile",
    profile_id: str = "Default",
    force_close_chrome: bool = False,
) -> tuple[bool, str]:
    """
    Import/clone session state, cookies, and Google accounts from an existing Chrome profile.
    
    This copies the decryption keys from 'Local State' and the session storage from the
    selected profile into the gateway's isolated user_data_dir.
    """
    source_base = source_user_data or find_chrome_user_data_dir()
    if not source_base or not source_base.exists():
        return False, f"Chrome user data directory not found at: {source_base}"

    source_profile = source_base / profile_id
    if not source_profile.exists():
        return False, f"Profile directory '{profile_id}' not found in {source_base}"

    target_base = Path(target_user_data).resolve()
    target_profile = target_base / "Default"  # Always set as Default profile in gateway

    # Check for active file locks
    if is_chrome_running():
        if force_close_chrome:
            logger.info("Closing Chrome to release database locks...")
            terminate_chrome_processes()
            if is_chrome_running():
                return False, "Could not terminate existing Chrome processes. Please close Chrome manually."
        else:
            return False, "Chrome is currently running and has locked the session database. Close Chrome or use force_close_chrome=True."

    try:
        target_base.mkdir(parents=True, exist_ok=True)
        target_profile.mkdir(parents=True, exist_ok=True)

        # 1. Copy and adapt 'Local State'
        source_local_state = source_base / "Local State"
        target_local_state = target_base / "Local State"
        if source_local_state.exists():
            try:
                with open(source_local_state, "r", encoding="utf-8") as f:
                    state = json.load(f)

                # Set last_used to Default
                if "profile" not in state:
                    state["profile"] = {}
                state["profile"]["last_used"] = "Default"

                # Ensure Default info entry exists
                info_cache = state.get("profile", {}).get("info_cache", {})
                if profile_id in info_cache:
                    info_cache["Default"] = info_cache[profile_id]

                with open(target_local_state, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
            except Exception as e:
                logger.warning(f"Could not adapt Local State JSON, performing direct copy: {e}")
                shutil.copy2(source_local_state, target_local_state)

        # 2. Copy individual auth files
        for fname in AUTH_FILES:
            src_file = source_profile / fname
            dst_file = target_profile / fname
            if src_file.exists():
                try:
                    shutil.copy2(src_file, dst_file)
                except Exception as e:
                    logger.debug(f"Skipped {fname}: {e}")

        # 3. Copy auth directories (excluding caches)
        for dname in AUTH_DIRS:
            src_dir = source_profile / dname
            dst_dir = target_profile / dname
            if src_dir.exists() and src_dir.is_dir():
                try:
                    if dst_dir.exists():
                        shutil.rmtree(dst_dir, ignore_errors=True)
                    shutil.copytree(
                        src_dir,
                        dst_dir,
                        ignore=shutil.ignore_patterns(
                            "*Cache*", "Cache", "Code Cache", "GPUCache", "ShaderCache", "blob_storage"
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Could not copy {dname}: {e}")

        return True, f"Successfully imported Chrome profile '{profile_id}' into {target_base}."
    except Exception as e:
        return False, f"Failed to import profile: {e}"


def launch_native_chrome_for_login(
    user_data_dir: Path | str,
    urls: list[str] | None = None,
    remote_debugging_port: int | None = None,
) -> subprocess.Popen:
    """
    Launch native Google Chrome directly as a normal desktop app with user_data_dir.
    
    Zero automation hooks, no Playwright CDP pipe, no navigator.webdriver.
    Bypasses all Google 'browser not secure' blocks and Cloudflare Turnstile bot tests.
    """
    chrome_path = find_browser_executable("chrome")
    if not chrome_path:
        raise RuntimeError(
            "Google Chrome executable not found. Please install Chrome or specify path manually."
        )

    target_dir = Path(user_data_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    default_urls = urls or [
        "https://chatgpt.com",
        "https://claude.ai",
        "https://chat.deepseek.com",
        "https://grok.com",
    ]

    cmd = [
        chrome_path,
        f"--user-data-dir={target_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-search-engine-choice-screen",
    ]

    if remote_debugging_port:
        cmd.append(f"--remote-debugging-port={remote_debugging_port}")

    cmd.extend(default_urls)
    logger.info(f"Launching native browser: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process
