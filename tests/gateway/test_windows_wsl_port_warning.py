from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import _build_windows_wsl_localhost_collision_warning


def test_warning_is_emitted_for_default_ports_in_wsl(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BROWSER_BRIDGE_ENABLED", "true")
    monkeypatch.delenv("HERMES_BROWSER_BRIDGE_HOST", raising=False)
    monkeypatch.delenv("HERMES_BROWSER_BRIDGE_PORT", raising=False)
    monkeypatch.delenv("API_SERVER_HOST", raising=False)
    monkeypatch.delenv("API_SERVER_PORT", raising=False)
    monkeypatch.setenv("BROWSER_CDP_PORT", "9222")

    config = GatewayConfig(
        platforms={Platform.API_SERVER: PlatformConfig(enabled=True, token="***")},
        sessions_dir=tmp_path / "sessions",
    )

    warning = _build_windows_wsl_localhost_collision_warning(
        config,
        os_name="posix",
        is_wsl=True,
    )

    assert warning is not None
    assert "BROWSER_CDP_PORT" in warning
    assert "8766/8643/9223" in warning


def test_warning_is_suppressed_when_ports_are_split(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BROWSER_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("HERMES_BROWSER_BRIDGE_PORT", "8766")
    monkeypatch.delenv("HERMES_BROWSER_BRIDGE_HOST", raising=False)
    monkeypatch.setenv("API_SERVER_PORT", "8643")
    monkeypatch.delenv("API_SERVER_HOST", raising=False)
    monkeypatch.setenv("BROWSER_CDP_PORT", "9223")

    config = GatewayConfig(
        platforms={Platform.API_SERVER: PlatformConfig(enabled=True, token="***")},
        sessions_dir=tmp_path / "sessions",
    )

    warning = _build_windows_wsl_localhost_collision_warning(
        config,
        os_name="posix",
        is_wsl=True,
    )

    assert warning is None
