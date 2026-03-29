import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from honcho_integration import cli as honcho_cli


def test_ensure_local_compose_env_uses_new_default_port(tmp_path, monkeypatch):
    docker_dir = tmp_path / "docker" / "honcho"
    docker_dir.mkdir(parents=True)
    compose_path = docker_dir / "compose.yaml"
    env_example = docker_dir / ".env.example"
    env_path = docker_dir / ".env"
    compose_path.write_text("name: honcho-local\n", encoding="utf-8")
    env_example.write_text("HONCHO_SOURCE_DIR=./honcho-src\n", encoding="utf-8")

    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_DIR", docker_dir)
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_COMPOSE", compose_path)
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_ENV_EXAMPLE", env_example)
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_ENV", env_path)

    _, written_env, base_url = honcho_cli._ensure_local_compose_env()

    assert written_env == env_path
    assert base_url == f"http://localhost:{honcho_cli.DEFAULT_LOCAL_HONCHO_PORT}"
    assert f"HONCHO_HTTP_PORT={honcho_cli.DEFAULT_LOCAL_HONCHO_PORT}" in env_path.read_text(
        encoding="utf-8"
    )


def test_resolve_docker_cli_prefers_reachable_engine(monkeypatch):
    monkeypatch.setattr(honcho_cli, "_docker_cli_candidates", lambda: ["docker", "docker.exe"])

    def fake_run(cmd, capture_output=True, text=True):
        class Result:
            def __init__(self, returncode):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = ""

        if cmd[0] == "docker":
            return Result(1)
        if cmd[0] == "docker.exe":
            return Result(0)
        return Result(1)

    monkeypatch.setattr(honcho_cli.subprocess, "run", fake_run)

    assert honcho_cli._resolve_docker_cli(require_engine=True) == "docker.exe"


def test_needs_windows_stage_for_wsl_paths(monkeypatch):
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_DIR", Path("/home/tester/honcho"))
    assert honcho_cli._needs_windows_stage("docker.exe") is True

    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_DIR", Path("/mnt/c/temp/honcho"))
    assert honcho_cli._needs_windows_stage("docker.exe") is False


def test_stage_local_honcho_project_copies_live_env(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "compose.yaml").write_text("name: honcho-local\n", encoding="utf-8")
    (source_dir / ".env").write_text("HONCHO_HTTP_PORT=8000\n", encoding="utf-8")
    (source_dir / ".env.example").write_text("HONCHO_HTTP_PORT=9000\n", encoding="utf-8")
    stage_root = tmp_path / "windows-temp"
    stage_root.mkdir()

    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_DIR", source_dir)
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_ENV", source_dir / ".env")
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_ENV_EXAMPLE", source_dir / ".env.example")
    monkeypatch.setattr(honcho_cli, "_windows_temp_dir", lambda: stage_root)

    staged = honcho_cli._stage_local_honcho_project_for_windows()

    assert staged == stage_root / honcho_cli.WINDOWS_STAGE_DIR_NAME
    assert (staged / ".env").read_text(encoding="utf-8") == "HONCHO_HTTP_PORT=8000\n"


def test_ensure_local_honcho_server_config_copies_template(tmp_path, monkeypatch):
    source_dir = tmp_path / "honcho-src"
    source_dir.mkdir()
    template = tmp_path / "config.toml.local.example"
    template.write_text("[summary]\nENABLED = false\n", encoding="utf-8")
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_CONFIG_TEMPLATE", template)

    written = honcho_cli._ensure_local_honcho_server_config(source_dir)

    assert written == source_dir / "config.toml"
    assert written.read_text(encoding="utf-8") == "[summary]\nENABLED = false\n"


def test_ensure_local_compose_env_copies_template_and_updates_values(tmp_path, monkeypatch):
    docker_dir = tmp_path / 'docker' / 'honcho'
    docker_dir.mkdir(parents=True)
    compose_path = docker_dir / 'compose.yaml'
    env_example = docker_dir / '.env.example'
    env_path = docker_dir / '.env'
    compose_path.write_text('name: honcho-local\n', encoding='utf-8')
    env_example.write_text(
        'HONCHO_SOURCE_DIR=./honcho-src\nHONCHO_HTTP_PORT=8000\n',
        encoding='utf-8',
    )

    monkeypatch.setattr(honcho_cli, 'LOCAL_HONCHO_DIR', docker_dir)
    monkeypatch.setattr(honcho_cli, 'LOCAL_HONCHO_COMPOSE', compose_path)
    monkeypatch.setattr(honcho_cli, 'LOCAL_HONCHO_ENV_EXAMPLE', env_example)
    monkeypatch.setattr(honcho_cli, 'LOCAL_HONCHO_ENV', env_path)

    source_path, written_env, base_url = honcho_cli._ensure_local_compose_env(port=8123)

    assert source_path == docker_dir / 'honcho-src'
    assert written_env == env_path
    assert base_url == 'http://localhost:8123'
    env_text = env_path.read_text(encoding='utf-8')
    assert 'HONCHO_SOURCE_DIR=./honcho-src' in env_text
    assert 'HONCHO_HTTP_PORT=8123' in env_text


def test_write_local_honcho_config_enables_local_only_mode(tmp_path, monkeypatch):
    hermes_home = tmp_path / 'hermes-home'
    hermes_home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(hermes_home))
    honcho_config_path = tmp_path / 'honcho.json'

    result_path = honcho_cli._write_local_honcho_config(
        'http://localhost:8000',
        workspace='workspace-local',
        peer_name='gille',
        memory_mode='hybrid',
        recall_mode='context',
        session_strategy='per-repo',
        honcho_config_path=honcho_config_path,
    )

    assert result_path == honcho_config_path

    hermes_cfg = yaml.safe_load((hermes_home / 'config.yaml').read_text(encoding='utf-8'))
    assert hermes_cfg['honcho']['enabled'] is True
    assert hermes_cfg['honcho']['local_only'] is True
    assert hermes_cfg['honcho']['base_url'] == 'http://localhost:8000'

    honcho_cfg = json.loads(honcho_config_path.read_text(encoding='utf-8'))
    hermes_host = honcho_cfg['hosts']['hermes']
    assert 'apiKey' not in honcho_cfg
    assert hermes_host['baseUrl'] == 'http://localhost:8000'
    assert hermes_host['enabled'] is True
    assert hermes_host['workspace'] == 'workspace-local'
    assert hermes_host['peerName'] == 'gille'
    assert hermes_host['recallMode'] == 'context'
    assert hermes_host['sessionStrategy'] == 'per-repo'


def test_write_local_honcho_server_config_syncs_nous_runtime(tmp_path, monkeypatch):
    source_dir = tmp_path / "honcho-src"
    source_dir.mkdir()
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HONCHO_HTTP_PORT=8420\nLLM_GEMINI_API_KEY=stale-key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "auxiliary": {
                "vision": {
                    "provider": "nous",
                    "model": "google/gemini-3-flash-preview",
                }
            }
        },
    )
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_nous_runtime_credentials",
        lambda min_key_ttl_seconds=0: {
            "provider": "nous",
            "api_key": "nous-test-key",
            "base_url": "https://inference-api.nousresearch.com/v1",
        },
    )

    config_path, profile = honcho_cli._write_local_honcho_server_config(source_dir, env_path)

    assert config_path == source_dir / "config.toml"
    assert profile["source_provider"] == "nous"
    assert profile["honcho_provider"] == "custom"
    assert profile["honcho_model"] == "google/gemini-3-flash-preview"
    config_text = config_path.read_text(encoding="utf-8")
    assert 'PROVIDER = "custom"' in config_text
    assert 'MODEL = "google/gemini-3-flash-preview"' in config_text

    env_text = env_path.read_text(encoding="utf-8")
    assert "LLM_OPENAI_COMPATIBLE_API_KEY=nous-test-key" in env_text
    assert "LLM_OPENAI_COMPATIBLE_BASE_URL=https://inference-api.nousresearch.com/v1" in env_text
    assert "LLM_GEMINI_API_KEY=stale-key" not in env_text


def test_collect_local_honcho_model_audit_reports_sync(tmp_path, monkeypatch):
    docker_dir = tmp_path / "docker" / "honcho"
    source_dir = docker_dir / "honcho-src"
    source_dir.mkdir(parents=True)
    compose_path = docker_dir / "compose.yaml"
    env_example = docker_dir / ".env.example"
    env_path = docker_dir / ".env"
    compose_path.write_text("name: honcho-local\n", encoding="utf-8")
    env_example.write_text("HONCHO_SOURCE_DIR=./honcho-src\nHONCHO_HTTP_PORT=8420\n", encoding="utf-8")
    env_path.write_text(
        "HONCHO_SOURCE_DIR=./honcho-src\n"
        "HONCHO_HTTP_PORT=8420\n"
        "LLM_OPENAI_COMPATIBLE_API_KEY=test-key\n"
        "LLM_OPENAI_COMPATIBLE_BASE_URL=https://inference-api.nousresearch.com/v1\n",
        encoding="utf-8",
    )
    (source_dir / "config.toml").write_text(
        """
[llm]
EMBEDDING_PROVIDER = "openai"

[deriver]
PROVIDER = "custom"
MODEL = "google/gemini-3-flash-preview"

[summary]
PROVIDER = "custom"
MODEL = "google/gemini-3-flash-preview"

[dream]
PROVIDER = "custom"
MODEL = "google/gemini-3-flash-preview"

[dialectic.levels.minimal]
PROVIDER = "custom"
MODEL = "google/gemini-3-flash-preview"

[dialectic.levels.low]
PROVIDER = "custom"
MODEL = "google/gemini-3-flash-preview"

[dialectic.levels.medium]
PROVIDER = "custom"
MODEL = "google/gemini-3-flash-preview"

[dialectic.levels.high]
PROVIDER = "custom"
MODEL = "google/gemini-3-flash-preview"

[dialectic.levels.max]
PROVIDER = "custom"
MODEL = "google/gemini-3-flash-preview"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_DIR", docker_dir)
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_COMPOSE", compose_path)
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_ENV_EXAMPLE", env_example)
    monkeypatch.setattr(honcho_cli, "LOCAL_HONCHO_ENV", env_path)
    monkeypatch.setattr(
        honcho_cli,
        "_resolve_local_honcho_runtime_profile",
        lambda: {
            "main_provider": "nous",
            "main_model": "openai/gpt-5.4-mini",
            "sync_label": "auxiliary.vision",
            "source_provider": "nous",
            "source_model": "google/gemini-3-flash-preview",
            "honcho_provider": "custom",
            "honcho_model": "google/gemini-3-flash-preview",
            "embedding_provider": "openai",
            "env_updates": {},
            "remove_env": [],
        },
    )

    audit = honcho_cli._collect_local_honcho_model_audit()

    assert audit["in_sync"] is True
    assert audit["mismatches"] == []
    assert audit["desired"]["main_model"] == "openai/gpt-5.4-mini"
    assert audit["desired"]["sync_label"] == "auxiliary.vision"
    assert audit["desired"]["source_provider"] == "nous"


def test_cmd_local_model_audit_prints_apply_hint_on_drift(capsys, monkeypatch):
    monkeypatch.setattr(
        honcho_cli,
        "_collect_local_honcho_model_audit",
        lambda **kwargs: {
            "base_url": "http://localhost:8420",
            "source_path": "/tmp/honcho-src",
            "env_path": "/tmp/.env",
            "desired": {
                "main_provider": "nous",
                "main_model": "openai/gpt-5.4-mini",
                "sync_label": "auxiliary.vision",
                "source_provider": "nous",
                "source_model": "google/gemini-3-flash-preview",
                "honcho_provider": "custom",
                "honcho_model": "google/gemini-3-flash-preview",
                "embedding_provider": "openai",
            },
            "current": {
                "routes": {
                    "summary": {"provider": "custom", "model": "old-model"},
                },
                "env_bridge": {
                    "LLM_OPENAI_COMPATIBLE_BASE_URL": "https://inference-api.nousresearch.com/v1",
                    "LLM_OPENAI_COMPATIBLE_API_KEY": True,
                    "LLM_GEMINI_API_KEY": False,
                },
            },
            "in_sync": False,
            "mismatches": ["summary: expected custom:google/gemini-3-flash-preview, found custom:old-model"],
        },
    )

    honcho_cli.cmd_local_model_audit(SimpleNamespace(port=None, source_dir=None, json=False))

    out = capsys.readouterr().out
    assert "Local Honcho model audit" in out
    assert "Main Hermes:" in out
    assert "Honcho sync:" in out
    assert "Status: drift detected" in out
    assert "hermes honcho local model-apply" in out
