# Windows Fixes Ledger

Updated: 2026-03-16
Scope: `hermes-agent` Windows install/startup/runtime reliability fixes validated from code state (not PR titles).

## Status Legend
- `done`: Implemented in code and verified by inspection.
- `partial`: Implemented but follow-up validation still recommended.
- `pending`: Not yet implemented in this repo state.

## Entries

### WF-001 - Package `minisweagent_path` for installed environments
- Status: `done`
- Problem: Global installs could fail at startup if `minisweagent_path.py` was not included in package metadata.
- Code evidence:
- `pyproject.toml` includes `minisweagent_path` in setuptools `py-modules`.
- `mini_swe_runner.py` and `tools/terminal_tool.py` import `minisweagent_path`.
- File refs:
- `pyproject.toml:91`
- `mini_swe_runner.py:47`
- `tools/terminal_tool.py:59`
- Validation notes:
- Import path is now represented in packaging metadata.

### WF-002 - Harden Windows console encoding at CLI startup
- Status: `done`
- Problem: `cmd.exe`/legacy code page output could crash on Unicode output.
- Code evidence:
- `_configure_console_encoding()` is defined and invoked at the beginning of `main()`.
- File refs:
- `hermes_cli/main.py:77`
- `hermes_cli/main.py:2434`
- Validation notes:
- Function is Windows-scoped and designed to fail safely.

### WF-003 - Honcho SDK installer fallback chain for uv/pip/ensurepip
- Status: `done`
- Problem: `hermes honcho setup` could fail in uv-managed or pip-missing environments.
- Code evidence:
- Installer helpers exist and are wired through `_ensure_sdk_installed()`.
- Includes `ensurepip --upgrade` fallback when pip launcher is missing.
- File refs:
- `honcho_integration/cli.py:59`
- `honcho_integration/cli.py:67`
- `honcho_integration/cli.py:75`
- `honcho_integration/cli.py:118`
- Tests:
- `tests/honcho_integration/test_cli.py:43`
- `tests/honcho_integration/test_cli.py:58`
- Validation notes:
- Helper and fallback flow are present in code and test coverage exists.

### WF-004 - Windows pytest timeout fixture behavior
- Status: `done`
- Problem: `SIGALRM` is unavailable on Windows.
- Code evidence:
- `tests/conftest.py` contains a guard to skip per-test alarm enforcement when `SIGALRM`/`alarm` are unavailable.
- File refs:
- `tests/conftest.py:111`
- Validation notes:
- Current fixture includes Windows-safe fallback.

### WF-005 - Windows terminal shell mode switching from Discord
- Status: `done`
- Problem: Windows shell mode switching existed in gateway command handling but needed a direct Discord slash entrypoint for easier use.
- Code evidence:
- Gateway `/terminal` command handler supports `powershell`, `wsl`, `auto`, and `cmd`.
- Discord slash command `/terminal` now dispatches to `/terminal <mode>` through the existing message command path.
- File refs:
- `gateway/run.py:3026`
- `gateway/platforms/discord.py:1441`
- Validation notes:
- `python -m py_compile gateway/platforms/discord.py` passed after slash-command integration.
- `python -m py_compile gateway/run.py` passed after `/terminal` handler restoration.

### WF-006 - Windows-native gateway lifecycle control and process reliability
- Status: `done`
- Problem: Windows gateway management relied on legacy WMIC scans and generic POSIX process kills, which could miss active gateway processes or leave child processes behind.
- Code evidence:
- `hermes_cli/gateway.py` now prefers PID-file verification (`gateway.status.get_running_pid`) and uses PowerShell CIM/WMI fallback scanning when needed.
- Windows process-tree termination now uses `taskkill /T` (and `/F` for forced cleanup).
- Added dedicated Windows lifecycle handlers:
- `windows_start_detached_gateway()`, `windows_stop_gateway()`, `windows_gateway_status()`.
- Added detached-start log reset helper:
- `reset_gateway_logs()` clears `~/.hermes/logs/gateway.log` and `gateway-error.log` before a fresh Windows start.
- `gateway_command()` now routes `start|stop|restart|status` through Windows-native handlers.
- File refs:
- `hermes_cli/gateway.py:30`
- `hermes_cli/gateway.py:129`
- `hermes_cli/gateway.py:1124`
- `hermes_cli/gateway.py:1166`
- `hermes_cli/gateway.py:1190`
- `hermes_cli/gateway.py:1658`
- Validation notes:
- `python -m py_compile hermes_cli/gateway.py` passed.
- `python -m py_compile gateway/platforms/discord.py hermes_cli/gateway.py` passed.
- re-run `python -m py_compile hermes_cli/gateway.py gateway/run.py` passed after detached-start log reset wiring.

### WF-007 - Gateway log inspection command parity (`hermes gateway logs`)
- Status: `done`
- Problem: Troubleshooting detached/background gateway behavior on Windows lacked a dedicated CLI log-tail command parity with personal repo workflow.
- Code evidence:
- Added log helpers in `hermes_cli/gateway.py` for reading/following `~/.hermes/logs/gateway.log` and optional `gateway-error.log`.
- Added `gateway_command()` support for `logs`.
- Added parser wiring for `hermes gateway logs` in `hermes_cli/main.py`.
- File refs:
- `hermes_cli/gateway.py:447`
- `hermes_cli/gateway.py:470`
- `hermes_cli/gateway.py:495`
- `hermes_cli/gateway.py:540`
- `hermes_cli/gateway.py:1862`
- `hermes_cli/main.py:2640`
- Validation notes:
- `python -m py_compile hermes_cli/gateway.py hermes_cli/main.py` passed.
- `python -m hermes_cli.main gateway logs --help` passed.
- `python -m hermes_cli.main gateway logs -n 2` returned recent log lines.

### WF-008 - UTF-8 default hardening for runtime file I/O
- Status: `done`
- Problem: Several runtime text reads/writes relied on platform-default encodings; on Windows this can be cp1252 and cause Unicode decode/encode issues with user content, logs, metadata, and caches.
- Code evidence:
- Added explicit UTF-8 for cron/delivery/status/gateway runtime state paths and update-notification artifacts.
- Added explicit UTF-8 for model metadata cache, timezone/config readers, and RL CLI config loader.
- Added explicit UTF-8 for Skills Hub cache/lock/taps/audit read-write paths.
- Added explicit UTF-8 for trajectory compressor YAML input + metrics output.
- File refs:
- `gateway/delivery.py:251`
- `gateway/delivery.py:264`
- `gateway/status.py:138`
- `gateway/status.py:152`
- `gateway/run.py:723`
- `gateway/run.py:4716`
- `gateway/run.py:4835`
- `agent/model_metadata.py:124`
- `agent/model_metadata.py:146`
- `hermes_time.py:54`
- `rl_cli.py:86`
- `cron/scheduler.py:272`
- `tools/skills_hub.py:461`
- `tools/skills_hub.py:470`
- `tools/skills_hub.py:2108`
- `trajectory_compressor.py:100`
- `trajectory_compressor.py:1168`
- Validation notes:
- `python -m py_compile` passed for all touched modules in this UTF-8 hardening batch.

### WF-009 - Windows cp1252-safe gateway logging output
- Status: `done`
- Problem: Gateway startup could raise `UnicodeEncodeError` in Windows cp1252 consoles when logger messages included Unicode symbols (for example `✓`/`✗`).
- Code evidence:
- Added `_harden_windows_console_logging()` to reconfigure stream handlers to UTF-8 with replacement on Windows; falls back to `_EncodingSafeStream` wrapper when stream reconfigure is unavailable.
- Converted gateway connection/disconnection logger markers to ASCII-safe log text.
- Configured rotating gateway log handlers with explicit `encoding="utf-8"` and `errors="replace"`.
- Adjusted duplicate-instance console print text to ASCII-safe wording.
- File refs:
- `gateway/run.py:577`
- `gateway/run.py:599`
- `gateway/run.py:5889`
- `gateway/run.py:1891`
- `gateway/run.py:1893`
- `gateway/run.py:1908`
- `gateway/run.py:2075`
- `gateway/run.py:2077`
- `gateway/run.py:5959`
- `gateway/run.py:5972`
- Validation notes:
- `python -m py_compile gateway/run.py` passed after logging hardening.

### WF-010 - Windows stale PID guard for `os.kill(pid, 0)` probe errors
- Status: `done`
- Problem: `hermes gateway start` could crash while checking existing gateway PID when `os.kill(pid, 0)` raised platform-specific `OSError` on Windows (`WinError 11`), instead of returning a normal stale/not-running result.
- Code evidence:
- Added `_is_process_alive(pid)` helper in `gateway/status.py` to normalize process-probe behavior:
- `PermissionError` -> treat as alive
- `ProcessLookupError` -> treat as not alive
- other `OSError` (including Windows-specific variants) -> treat as not alive
- Updated both lock staleness checks and `get_running_pid()` to use `_is_process_alive()`.
- Added `pid <= 0` guard in `get_running_pid()` to clean invalid PID records.
- File refs:
- `gateway/status.py:59`
- `gateway/status.py:279`
- `gateway/status.py:344`
- Validation notes:
- `python -m py_compile gateway/status.py` passed after guard hardening.

### WF-011 - Enforce secure F5 JWT secret length to avoid runtime key warnings
- Status: `done`
- Problem: F5 TTS token minting could emit `InsecureKeyLengthWarning` from PyJWT when `F5TTS_SECRET_KEY` was under 32 bytes for HS256.
- Code evidence:
- Added explicit preflight length enforcement in `tools/tts_tool.py` before JWT encode (`>= 32` bytes, UTF-8 count).
- Returns actionable config error with required minimum and remediation text.
- Updated user-facing config guidance and env var description to document the 32+ byte requirement.
- File refs:
- `tools/tts_tool.py:82`
- `tools/tts_tool.py:431`
- `cli-config.yaml.example:642`
- `hermes_cli/config.py:542`
- Validation notes:
- `python -m py_compile tools/tts_tool.py hermes_cli/config.py` passed after enforcement and docs updates.

### WF-012 - Inline startup log streaming for Windows `gateway start/restart`
- Status: `done`
- Problem: `hermes gateway start` on Windows launches a detached gateway console, leaving the invoking terminal with no immediate runtime feedback unless users manually open log files.
- Code evidence:
- Added `stream_gateway_startup_logs()` in `hermes_cli/gateway.py` to stream `gateway.log`/`gateway-error.log` updates inline for an initial startup window.
- Updated `windows_start_detached_gateway()` to invoke startup streaming after launch success/path checks.
- Added `--no-startup-stream` CLI flag for `gateway start` and `gateway restart` to opt out.
- Wired `gateway_command()` Windows `start`/`restart` paths to pass `stream_startup` behavior from CLI args.
- File refs:
- `hermes_cli/gateway.py`
- `hermes_cli/main.py`
- Validation notes:
- `python -m py_compile hermes_cli/gateway.py hermes_cli/main.py` passed after startup-stream integration.

### WF-013 - Reduce Windows startup stream verbosity to concise status summary
- Status: `done`
- Problem: Initial inline startup stream emitted full log firehose and was too noisy for normal `hermes gateway start` UX.
- Code evidence:
- Updated `stream_gateway_startup_logs()` in `hermes_cli/gateway.py` to collect startup lines and print a compact summary (running state, connected platforms, slash sync, browser bridge, cron ticker) instead of raw full-line streaming.
- Warnings/errors are still surfaced under an `Issues` section for visibility.
- Added duplicate-issue suppression so the same warning line (for example when mirrored in both `gateway.log` and `errors.log`) appears once in startup summary output.
- Reduced default startup watch window from 12s to 8s.
- File refs:
- `hermes_cli/gateway.py`
- Validation notes:
- `python -m py_compile hermes_cli/gateway.py` passed after compact-summary tuning.

### WF-014 - Browser bridge shutdown coroutine warning + Ctrl+C traceback hardening
- Status: `done`
- Problem:
- During gateway shutdown/interruption on Windows, browser bridge requests could race with loop teardown and trigger:
- `RuntimeWarning: coroutine 'GatewayRunner._handle_browser_bridge_request' was never awaited`
- Foreground `gateway run` could also print large `KeyboardInterrupt` tracebacks on Ctrl+C.
- Code evidence:
- Hardened browser bridge request scheduling:
- `BrowserBridgeServer.run_payload()` now safely handles awaitables, checks loop availability before scheduling, closes unscheduled coroutines, and cancels pending futures on failure paths.
- Browser bridge HTTP handler now maps loop-unavailable runtime errors to clean `503` responses (instead of full exception traces) during shutdown races.
- Hardened gateway shutdown wait:
- `start_gateway()` now catches `asyncio.CancelledError` around `wait_for_shutdown()` and attempts graceful stop before final cleanup.
- Hardened CLI foreground run UX:
- `run_gateway()` now catches `KeyboardInterrupt` and exits cleanly with a short stop message instead of traceback spam.
- File refs:
- `gateway/browser_bridge.py`
- `gateway/run.py`
- `hermes_cli/gateway.py`
- Validation notes:
- `python -m py_compile gateway/browser_bridge.py gateway/run.py hermes_cli/gateway.py` passed.

### WF-015 - STT provider pinning for Windows voice capture reliability
- Status: `done`
- Problem:
- Voice capture transcription could unexpectedly route to local `faster-whisper` on Windows (triggering CUDA DLL/runtime errors) when `stt.provider` was unset or when explicit OpenAI provider was silently downgraded by fallback logic.
- Code evidence:
- Updated STT provider resolution in `tools/transcription_tools.py`:
- If `stt.provider` is unset, infer provider from `stt.model` when possible (`whisper-1`/OpenAI models -> OpenAI, Groq model names -> Groq).
- If `stt.provider: openai` is explicitly set, keep provider as OpenAI instead of silently falling back to local.
- Runtime config updated to explicitly pin OpenAI STT in user config (`~/.hermes/config.yaml`).
- File refs:
- `tools/transcription_tools.py`
- `C:\Users\btgil\.hermes\config.yaml`
- Validation notes:
- `python -m py_compile tools/transcription_tools.py` passed.

## Open Follow-ups
- Re-run targeted pytest in your preferred Windows environment after current merge work settles to confirm no hidden cross-fixture assumptions remain.
- Keep this ledger as source-of-truth for Windows stability fixes; append entries instead of rewriting history.
