from pathlib import Path
import tempfile
import pytest

from app.browser.profile_importer import (
    find_browser_executable,
    find_chrome_user_data_dir,
    get_available_profiles,
    is_chrome_running,
    import_chrome_profile,
)


def test_find_browser_executable():
    exe = find_browser_executable("chrome")
    assert exe is not None
    assert Path(exe).exists()
    assert "chrome" in exe.lower() or "msedge" in exe.lower()


def test_find_chrome_user_data_dir():
    user_data = find_chrome_user_data_dir()
    assert user_data is not None
    assert user_data.exists()


def test_get_available_profiles():
    profiles = get_available_profiles()
    assert isinstance(profiles, list)
    assert len(profiles) > 0
    first = profiles[0]
    assert "id" in first
    assert "name" in first
    assert "email" in first


def test_is_chrome_running_returns_bool():
    res = is_chrome_running()
    assert isinstance(res, bool)


def test_import_profile_dry_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "test_profile"
        # Test importing from existing user data dir with force_close_chrome=False
        # If chrome is running, should return False and message; if not running, True
        ok, msg = import_chrome_profile(target_user_data=target, force_close_chrome=False)
        assert isinstance(ok, bool)
        assert isinstance(msg, str)
