"""Tests for browser target resolution in /browser connect."""

from unittest.mock import patch

from cli import HermesCLI


def test_resolve_browser_alias_to_deterministic_port():
    with patch("tools.browser_tool._is_wsl_runtime", return_value=False):
        cdp_url, browser, err = HermesCLI._resolve_browser_connect_target(
            raw_arg="comet",
            default_port=9222,
            default_browser="auto",
            default_cdp_url="ws://localhost:9222",
        )
    assert err == ""
    assert cdp_url == "ws://localhost:9226"
    assert browser == "comet"


def test_resolve_numeric_port_target():
    cdp_url, browser, err = HermesCLI._resolve_browser_connect_target(
        raw_arg="9333",
        default_port=9222,
        default_browser="edge",
        default_cdp_url="ws://localhost:9222",
    )
    assert err == ""
    assert cdp_url == "ws://localhost:9333"
    assert browser == "edge"


def test_resolve_host_port_target_without_scheme():
    cdp_url, browser, err = HermesCLI._resolve_browser_connect_target(
        raw_arg="127.0.0.1:9555",
        default_port=9222,
        default_browser="chrome",
        default_cdp_url="ws://localhost:9222",
    )
    assert err == ""
    assert cdp_url == "ws://127.0.0.1:9555"
    assert browser == "chrome"


def test_resolve_unknown_target_returns_error():
    cdp_url, browser, err = HermesCLI._resolve_browser_connect_target(
        raw_arg="totally-not-a-browser",
        default_port=9222,
        default_browser="chrome",
        default_cdp_url="ws://localhost:9222",
    )
    assert cdp_url == ""
    assert browser == ""
    assert "Unknown browser target" in err


def test_runtime_aware_default_port_uses_wsl_shift():
    with patch("tools.browser_tool._is_wsl_runtime", return_value=True):
        assert HermesCLI._default_cdp_port_for_browser("auto") == 9223
