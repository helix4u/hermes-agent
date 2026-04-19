"""Tests for web backend client configuration and singleton behavior.

Coverage:
  _get_firecrawl_client() — configuration matrix, singleton caching,
  constructor failure recovery, return value verification, edge cases.
  _get_backend() — backend selection logic with env var combinations.
  _get_parallel_client() — Parallel client configuration, singleton caching.
  check_web_api_key() — unified availability check.
"""

import json
import os
import time
import pytest
from unittest.mock import patch, MagicMock


class TestFirecrawlClientConfig:
    """Test suite for Firecrawl client initialization."""

    def setup_method(self):
        """Reset client and env vars before each test."""
        import tools.web_tools
        tools.web_tools._firecrawl_client = None
        for key in ("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL"):
            os.environ.pop(key, None)

    def teardown_method(self):
        """Reset client after each test."""
        import tools.web_tools
        tools.web_tools._firecrawl_client = None
        for key in ("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL"):
            os.environ.pop(key, None)

    # ── Configuration matrix ─────────────────────────────────────────

    def test_cloud_mode_key_only(self):
        """API key without URL → cloud Firecrawl."""
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}):
            with patch("tools.web_tools.Firecrawl") as mock_fc:
                from tools.web_tools import _get_firecrawl_client
                result = _get_firecrawl_client()
                mock_fc.assert_called_once_with(api_key="fc-test")
                assert result is mock_fc.return_value

    def test_self_hosted_with_key(self):
        """Both key + URL → self-hosted with auth."""
        with patch.dict(os.environ, {
            "FIRECRAWL_API_KEY": "fc-test",
            "FIRECRAWL_API_URL": "http://localhost:3002",
        }):
            with patch("tools.web_tools.Firecrawl") as mock_fc:
                from tools.web_tools import _get_firecrawl_client
                result = _get_firecrawl_client()
                mock_fc.assert_called_once_with(
                    api_key="fc-test", api_url="http://localhost:3002"
                )
                assert result is mock_fc.return_value

    def test_self_hosted_no_key(self):
        """URL only, no key → self-hosted without auth."""
        with patch.dict(os.environ, {"FIRECRAWL_API_URL": "http://localhost:3002"}):
            with patch("tools.web_tools.Firecrawl") as mock_fc:
                from tools.web_tools import _get_firecrawl_client
                result = _get_firecrawl_client()
                mock_fc.assert_called_once_with(api_url="http://localhost:3002")
                assert result is mock_fc.return_value

    def test_no_config_raises_with_helpful_message(self):
        """Neither key nor URL → ValueError with guidance."""
        with patch("tools.web_tools.Firecrawl"):
            from tools.web_tools import _get_firecrawl_client
            with pytest.raises(ValueError, match="FIRECRAWL_API_KEY"):
                _get_firecrawl_client()

    # ── Singleton caching ────────────────────────────────────────────

    def test_singleton_returns_same_instance(self):
        """Second call returns cached client without re-constructing."""
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}):
            with patch("tools.web_tools.Firecrawl") as mock_fc:
                from tools.web_tools import _get_firecrawl_client
                client1 = _get_firecrawl_client()
                client2 = _get_firecrawl_client()
                assert client1 is client2
                mock_fc.assert_called_once()  # constructed only once

    def test_constructor_failure_allows_retry(self):
        """If Firecrawl() raises, next call should retry (not return None)."""
        import tools.web_tools
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}):
            with patch("tools.web_tools.Firecrawl") as mock_fc:
                mock_fc.side_effect = [RuntimeError("init failed"), MagicMock()]
                from tools.web_tools import _get_firecrawl_client

                with pytest.raises(RuntimeError):
                    _get_firecrawl_client()

                # Client stayed None, so retry should work
                assert tools.web_tools._firecrawl_client is None
                result = _get_firecrawl_client()
                assert result is not None

    # ── Edge cases ───────────────────────────────────────────────────

    def test_empty_string_key_treated_as_absent(self):
        """FIRECRAWL_API_KEY='' should not be passed as api_key."""
        with patch.dict(os.environ, {
            "FIRECRAWL_API_KEY": "",
            "FIRECRAWL_API_URL": "http://localhost:3002",
        }):
            with patch("tools.web_tools.Firecrawl") as mock_fc:
                from tools.web_tools import _get_firecrawl_client
                _get_firecrawl_client()
                # Empty string is falsy, so only api_url should be passed
                mock_fc.assert_called_once_with(api_url="http://localhost:3002")

    def test_empty_string_key_no_url_raises(self):
        """FIRECRAWL_API_KEY='' with no URL → should raise."""
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": ""}):
            with patch("tools.web_tools.Firecrawl"):
                from tools.web_tools import _get_firecrawl_client
                with pytest.raises(ValueError):
                    _get_firecrawl_client()


class TestBackendSelection:
    """Test suite for _get_backend() backend selection logic.

    The backend is configured via config.yaml (web.backend), set by
    ``hermes tools``.  Falls back to key-based detection for legacy/manual
    setups.
    """

    _ENV_KEYS = ("PARALLEL_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "TAVILY_API_KEY")

    def setup_method(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)

    def teardown_method(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)

    # ── Config-based selection (web.backend in config.yaml) ───────────

    def test_config_parallel(self):
        """web.backend=parallel in config → 'parallel' regardless of keys."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "parallel"}):
            assert _get_backend() == "parallel"

    def test_config_firecrawl(self):
        """web.backend=firecrawl in config → 'firecrawl' even if Parallel key set."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "firecrawl"}), \
             patch.dict(os.environ, {"PARALLEL_API_KEY": "test-key"}):
            assert _get_backend() == "firecrawl"

    def test_config_tavily(self):
        """web.backend=tavily in config → 'tavily' regardless of other keys."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "tavily"}):
            assert _get_backend() == "tavily"

    def test_config_tavily_overrides_env_keys(self):
        """web.backend=tavily in config → 'tavily' even if Firecrawl key set."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "tavily"}), \
             patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}):
            assert _get_backend() == "tavily"

    def test_config_case_insensitive(self):
        """web.backend=Parallel (mixed case) → 'parallel'."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "Parallel"}):
            assert _get_backend() == "parallel"

    def test_config_tavily_case_insensitive(self):
        """web.backend=Tavily (mixed case) → 'tavily'."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "Tavily"}):
            assert _get_backend() == "tavily"

    # ── Fallback (no web.backend in config) ───────────────────────────

    def test_fallback_parallel_only_key(self):
        """Only PARALLEL_API_KEY set → 'parallel'."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch.dict(os.environ, {"PARALLEL_API_KEY": "test-key"}):
            assert _get_backend() == "parallel"

    def test_fallback_tavily_only_key(self):
        """Only TAVILY_API_KEY set → 'tavily'."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}):
            assert _get_backend() == "tavily"

    def test_fallback_tavily_with_firecrawl_prefers_firecrawl(self):
        """Tavily + Firecrawl keys, no config → 'firecrawl' (backward compat)."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test", "FIRECRAWL_API_KEY": "fc-test"}):
            assert _get_backend() == "firecrawl"

    def test_fallback_tavily_with_parallel_prefers_parallel(self):
        """Tavily + Parallel keys, no config → 'parallel' (Parallel takes priority over Tavily)."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test", "PARALLEL_API_KEY": "par-test"}):
            # Parallel + no Firecrawl → parallel
            assert _get_backend() == "parallel"

    def test_fallback_both_keys_defaults_to_firecrawl(self):
        """Both keys set, no config → 'firecrawl' (backward compat)."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch.dict(os.environ, {"PARALLEL_API_KEY": "test-key", "FIRECRAWL_API_KEY": "fc-test"}):
            assert _get_backend() == "firecrawl"

    def test_fallback_firecrawl_only_key(self):
        """Only FIRECRAWL_API_KEY set → 'firecrawl'."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}):
            assert _get_backend() == "firecrawl"

    def test_fallback_no_keys_defaults_to_firecrawl(self):
        """No keys, no config → 'firecrawl' (will fail at client init)."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}):
            assert _get_backend() == "firecrawl"

    def test_invalid_config_falls_through_to_fallback(self):
        """web.backend=invalid → ignored, uses key-based fallback."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "nonexistent"}), \
             patch.dict(os.environ, {"PARALLEL_API_KEY": "test-key"}):
            assert _get_backend() == "parallel"


class TestParallelClientConfig:
    """Test suite for Parallel client initialization."""

    def setup_method(self):
        import tools.web_tools
        tools.web_tools._parallel_client = None
        os.environ.pop("PARALLEL_API_KEY", None)

    def teardown_method(self):
        import tools.web_tools
        tools.web_tools._parallel_client = None
        os.environ.pop("PARALLEL_API_KEY", None)

    def test_creates_client_with_key(self):
        """PARALLEL_API_KEY set → creates Parallel client."""
        with patch.dict(os.environ, {"PARALLEL_API_KEY": "test-key"}):
            from tools.web_tools import _get_parallel_client
            from parallel import Parallel
            client = _get_parallel_client()
            assert client is not None
            assert isinstance(client, Parallel)

    def test_no_key_raises_with_helpful_message(self):
        """No PARALLEL_API_KEY → ValueError with guidance."""
        from tools.web_tools import _get_parallel_client
        with pytest.raises(ValueError, match="PARALLEL_API_KEY"):
            _get_parallel_client()

    def test_singleton_returns_same_instance(self):
        """Second call returns cached client."""
        with patch.dict(os.environ, {"PARALLEL_API_KEY": "test-key"}):
            from tools.web_tools import _get_parallel_client
            client1 = _get_parallel_client()
            client2 = _get_parallel_client()
            assert client1 is client2


class TestCheckWebApiKey:
    """Test suite for check_web_api_key() unified availability check."""

    _ENV_KEYS = ("PARALLEL_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "TAVILY_API_KEY")

    def setup_method(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)

    def teardown_method(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)

    def test_parallel_key_only(self):
        with patch.dict(os.environ, {"PARALLEL_API_KEY": "test-key"}):
            from tools.web_tools import check_web_api_key
            assert check_web_api_key() is True

    def test_firecrawl_key_only(self):
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}):
            from tools.web_tools import check_web_api_key
            assert check_web_api_key() is True

    def test_firecrawl_url_only(self):
        with patch.dict(os.environ, {"FIRECRAWL_API_URL": "http://localhost:3002"}):
            from tools.web_tools import check_web_api_key
            assert check_web_api_key() is True

    def test_tavily_key_only(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}):
            from tools.web_tools import check_web_api_key
            assert check_web_api_key() is True

    def test_no_keys_returns_false(self):
        from tools.web_tools import check_web_api_key
        assert check_web_api_key() is False

    def test_both_keys_returns_true(self):
        with patch.dict(os.environ, {
            "PARALLEL_API_KEY": "test-key",
            "FIRECRAWL_API_KEY": "fc-test",
        }):
            from tools.web_tools import check_web_api_key
            assert check_web_api_key() is True

    def test_all_three_keys_returns_true(self):
        with patch.dict(os.environ, {
            "PARALLEL_API_KEY": "test-key",
            "FIRECRAWL_API_KEY": "fc-test",
            "TAVILY_API_KEY": "tvly-test",
        }):
            from tools.web_tools import check_web_api_key
            assert check_web_api_key() is True


class TestWebTimeoutConfig:
    """Test suite for configurable web timeouts and retry budgets."""

    def test_web_request_timeout_comes_from_config(self):
        from tools.web_tools import _get_web_request_timeout

        with patch(
            "tools.web_tools._load_web_config",
            return_value={"request_timeout": 42},
        ):
            assert _get_web_request_timeout() == 42.0

    def test_web_request_timeout_env_override_wins(self):
        from tools.web_tools import _get_web_request_timeout

        with patch.dict(os.environ, {"WEB_REQUEST_TIMEOUT": "7.5"}):
            with patch(
                "tools.web_tools._load_web_config",
                return_value={"request_timeout": 42},
            ):
                assert _get_web_request_timeout() == 7.5

    def test_web_extract_llm_timeout_and_retries_come_from_config(self):
        from tools.web_tools import (
            _get_web_extract_llm_max_retries,
            _get_web_extract_llm_timeout,
        )

        config = {
            "auxiliary": {
                "web_extract": {
                    "timeout": 18,
                    "max_retries": 3,
                }
            }
        }

        with patch("hermes_cli.config.load_config", return_value=config):
            assert _get_web_extract_llm_timeout() == 18.0
            assert _get_web_extract_llm_max_retries() == 3


@pytest.mark.asyncio
async def test_web_extract_times_out_slow_firecrawl_scrape(monkeypatch):
    from tools import web_tools

    class SlowFirecrawlClient:
        def scrape(self, url, formats):
            time.sleep(0.05)
            return {"markdown": "late result", "metadata": {"sourceURL": url}}

    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    monkeypatch.setattr(web_tools, "check_website_access", lambda url: None)
    monkeypatch.setattr(web_tools, "_get_firecrawl_client", lambda: SlowFirecrawlClient())
    monkeypatch.setattr(web_tools, "_get_web_request_timeout", lambda: 0.01)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com"],
            use_llm_processing=False,
        )
    )

    assert "timed out" in result["results"][0]["error"].lower()
    assert result["results"][0]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_web_extract_llm_retry_budget_is_bounded(monkeypatch):
    from tools import web_tools

    calls = []

    async def fake_call_llm(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("llm timeout")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(web_tools, "async_call_llm", fake_call_llm)
    monkeypatch.setattr(web_tools, "_get_web_extract_llm_timeout", lambda: 9.0)
    monkeypatch.setattr(web_tools, "_get_web_extract_llm_max_retries", lambda: 2)
    monkeypatch.setattr(web_tools.asyncio, "sleep", fake_sleep)

    with pytest.raises(TimeoutError, match="llm timeout"):
        await web_tools._call_summarizer_llm("content", "", None)

    assert len(calls) == 2
    assert all(call["timeout"] == 9.0 for call in calls)


@pytest.mark.asyncio
async def test_process_content_with_llm_skips_medium_content_by_default():
    from tools.web_tools import process_content_with_llm

    content = "x" * 8500
    result = await process_content_with_llm(content)

    assert result is None
