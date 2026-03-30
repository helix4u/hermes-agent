"""Regression tests for browser session cleanup and screenshot recovery."""

from unittest.mock import patch


class TestScreenshotPathRecovery:
    def test_extracts_standard_absolute_path(self):
        from tools.browser_tool import _extract_screenshot_path_from_text

        assert (
            _extract_screenshot_path_from_text("Screenshot saved to /tmp/foo.png")
            == "/tmp/foo.png"
        )

    def test_extracts_quoted_absolute_path(self):
        from tools.browser_tool import _extract_screenshot_path_from_text

        assert (
            _extract_screenshot_path_from_text(
                "Screenshot saved to '/Users/david/.hermes/browser_screenshots/shot.png'"
            )
            == "/Users/david/.hermes/browser_screenshots/shot.png"
        )


class TestBrowserCleanup:
    def setup_method(self):
        from tools import browser_tool

        self.browser_tool = browser_tool
        self.orig_active_sessions = browser_tool._active_sessions.copy()
        self.orig_session_last_activity = browser_tool._session_last_activity.copy()
        self.orig_recording_sessions = browser_tool._recording_sessions.copy()
        self.orig_cleanup_done = browser_tool._cleanup_done

    def teardown_method(self):
        self.browser_tool._active_sessions.clear()
        self.browser_tool._active_sessions.update(self.orig_active_sessions)
        self.browser_tool._session_last_activity.clear()
        self.browser_tool._session_last_activity.update(self.orig_session_last_activity)
        self.browser_tool._recording_sessions.clear()
        self.browser_tool._recording_sessions.update(self.orig_recording_sessions)
        self.browser_tool._cleanup_done = self.orig_cleanup_done

    def test_cleanup_browser_clears_tracking_state(self):
        browser_tool = self.browser_tool
        browser_tool._active_sessions["task-1"] = {
            "session_name": "sess-1",
            "bb_session_id": None,
        }
        browser_tool._session_last_activity["task-1"] = 123.0

        with (
            patch("tools.browser_tool._maybe_stop_recording") as mock_stop,
            patch(
                "tools.browser_tool._run_browser_command",
                return_value={"success": True},
            ) as mock_run,
            patch("tools.browser_tool.os.path.exists", return_value=False),
        ):
            browser_tool.cleanup_browser("task-1")

        assert "task-1" not in browser_tool._active_sessions
        assert "task-1" not in browser_tool._session_last_activity
        mock_stop.assert_called_once_with("task-1")
        mock_run.assert_called_once_with("task-1", "close", [], timeout=10)

    def test_cleanup_browser_terminates_spawned_cdp_browser(self):
        browser_tool = self.browser_tool
        browser_tool._active_sessions["task-cdp"] = {
            "session_name": "sess-cdp",
            "bb_session_id": None,
            "spawned_cdp_browser": {"pid": 4242, "profile_dir": "/tmp/hermes-cdp"},
        }
        browser_tool._session_last_activity["task-cdp"] = 456.0

        with (
            patch("tools.browser_tool._maybe_stop_recording"),
            patch("tools.browser_tool._run_browser_command", return_value={"success": True}),
            patch("tools.browser_tool._terminate_spawned_cdp_browser") as mock_terminate,
            patch("tools.browser_tool.os.path.exists", return_value=False),
        ):
            browser_tool.cleanup_browser("task-cdp")

        mock_terminate.assert_called_once_with({"pid": 4242, "profile_dir": "/tmp/hermes-cdp"})

    def test_get_session_info_tracks_auto_launched_cdp_browser(self):
        browser_tool = self.browser_tool

        with (
            patch("tools.browser_tool._start_browser_cleanup_thread"),
            patch("tools.browser_tool._update_session_activity"),
            patch("tools.browser_tool._get_cdp_override", return_value="ws://localhost:9223"),
            patch(
                "tools.browser_tool._ensure_local_cdp_browser",
                return_value=(
                    "ws://localhost:9223/devtools/browser/abc",
                    {"pid": 9191, "profile_dir": "/tmp/hermes-cdp"},
                ),
            ),
            patch.dict(browser_tool._active_sessions, {}, clear=True),
        ):
            session_info = browser_tool._get_session_info("task-cdp-autolaunch")

        assert session_info["cdp_url"] == "ws://localhost:9223/devtools/browser/abc"
        assert session_info["spawned_cdp_browser"]["pid"] == 9191

    def test_browser_close_delegates_to_cleanup_browser(self):
        import json

        browser_tool = self.browser_tool
        browser_tool._active_sessions["task-2"] = {"session_name": "sess-2"}

        with patch("tools.browser_tool.cleanup_browser") as mock_cleanup:
            result = json.loads(browser_tool.browser_close("task-2"))

        assert result == {"success": True, "closed": True}
        mock_cleanup.assert_called_once_with("task-2")

    def test_emergency_cleanup_clears_all_tracking_state(self):
        browser_tool = self.browser_tool
        browser_tool._cleanup_done = False
        browser_tool._active_sessions["task-1"] = {"session_name": "sess-1"}
        browser_tool._active_sessions["task-2"] = {"session_name": "sess-2"}
        browser_tool._session_last_activity["task-1"] = 1.0
        browser_tool._session_last_activity["task-2"] = 2.0
        browser_tool._recording_sessions.update({"task-1", "task-2"})

        with patch("tools.browser_tool.cleanup_all_browsers") as mock_cleanup_all:
            browser_tool._emergency_cleanup_all_sessions()

        mock_cleanup_all.assert_called_once_with()
        assert browser_tool._active_sessions == {}
        assert browser_tool._session_last_activity == {}
        assert browser_tool._recording_sessions == set()
        assert browser_tool._cleanup_done is True
