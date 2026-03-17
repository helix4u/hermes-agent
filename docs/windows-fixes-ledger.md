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

### WF-016 - Windows browser CLI resolution hardening (`npx.CMD` + PATH separator safety)
- Status: `done`
- Problem:
- Browser tool calls could fail immediately on Windows with `[WinError 2] The system cannot find the file specified` when `agent-browser` was invoked via `npx` fallback.
- Root cause:
- Fallback execution used `npx` as a bare command token (not absolute `npx.CMD`), and PATH mutation logic used Unix `:` separators instead of `os.pathsep`.
- Code evidence:
- `tools/browser_tool.py` now resolves browser launcher commands as tokenized command parts:
- global install -> `[<absolute-agent-browser-path>]`
- local install -> supports both `node_modules/.bin/agent-browser` and `agent-browser.cmd`
- fallback -> `[<absolute-npx-path>, "agent-browser"]`
- `_run_browser_command()` now uses `os.pathsep` for PATH split/join and normalized dedupe.
- File refs:
- `tools/browser_tool.py`
- Validation notes:
- `python -m py_compile tools/browser_tool.py` passed.
- Direct runtime probe passed:
- `.venv\Scripts\python -c "import tools.browser_tool as b, subprocess; p=b._find_agent_browser(); print(p); print(subprocess.run(p+['--version'], capture_output=True, text=True, timeout=20).returncode)"` -> resolved `npx.CMD` path and returned `0`.

### WF-017 - Sidecar turn crash containment (prevent gateway "poof" on fatal turn errors)
- Status: `done`
- Problem:
- During browser-sidecar image turns, fatal exceptions in the turn pipeline could bubble as cancellation/fatal errors and terminate the gateway process instead of failing only the turn.
- Code evidence:
- Hardened sidecar turn execution in `gateway/run.py`:
- `_handle_browser_bridge_send()` now treats `CancelledError` as an interrupted turn state and records progress cleanly.
- Added `BaseException` guard in sidecar turn runner so fatal tool/runtime exceptions are captured as turn failure instead of process-killing errors.
- Hardened top-level message handling in `gateway/run.py`:
- Added `BaseException` guard in `_handle_message()` so fatal worker errors are returned as turn errors while keeping gateway alive.
- Added cancellation-resume guard in `start_gateway()`:
- if cancellation hits while sidecar turns are active and no shutdown was requested, gateway uncancels/resumes wait instead of immediately stopping.
- Hardened browser bridge HTTP handling in `gateway/browser_bridge.py`:
- `do_POST()` now catches `BaseException` and returns a controlled 500 error response rather than allowing request-thread fatal exits.
- File refs:
- `gateway/run.py`
- `gateway/browser_bridge.py`
- Validation notes:
- `python -m py_compile gateway/run.py gateway/browser_bridge.py hermes_cli/gateway.py` passed after patch.

### WF-018 - CLI exit crash guard + Windows Chrome launch command hardening
- Status: `done`
- Problem:
- Exiting interactive `hermes` could print a full traceback and abort when memory flush hit provider/bootstrap errors (for example SSL provider init failures).
- `/browser connect` on Windows could fail to auto-launch robustly and manual fallback text was too weak (`chrome.exe ...`) for non-PATH contexts.
- Code evidence:
- Hardened exit/new-session memory flush guards in `cli.py`:
- `flush_memories(...)` calls in both `new_session()` and CLI shutdown finalizer now catch `BaseException` to prevent shutdown-time crashes.
- Hardened `/browser connect` launcher in `cli.py`:
- `_try_launch_chrome_debug()` now includes Windows executable discovery (Program Files/LOCALAPPDATA + PATH fallbacks), detached launch flags, and explicit `--user-data-dir`.
- Manual fallback command now prints a fully-qualified Chrome path + remote debugging args for Windows.
- Added connection truthfulness guard:
- `/browser connect` no longer marks connected when localhost CDP endpoint is still unreachable.
- File refs:
- `cli.py`
- Validation notes:
- `python -m py_compile cli.py` passed.

### WF-019 - `agent-browser` daemon TCP bind failure on Windows (`os error 10013`)
- Status: `done`
- Problem:
- Browser tool commands (`open`, `snapshot`, local and CDP modes) fail before navigation with:
- `Daemon process exited during startup`
- `Failed to bind TCP ... (os error 10013)`
- Code evidence:
- Added Windows daemon stream-port hardening in `tools/browser_tool.py`:
- default `AGENT_BROWSER_STREAM_PORT=0` when unset on Windows.
- detects bind error signature (`Failed to bind TCP` + `10013`/permissions text) and retries command with safe stream-port override.
- retry uses `"0"` if caller had an explicit conflicting port; otherwise allocates a free local port.
- Added regression tests:
- `tests/tools/test_browser_windows_stream_port.py`
- Validation notes:
- Direct host reproduction before fix:
- `agent-browser --session hermes_bindprobe --json open https://example.com` -> bind `10013`.
- Direct host validation with stream-port override:
- `set AGENT_BROWSER_STREAM_PORT=0 && agent-browser --session hermes_bindprobe2 --json open https://example.com` -> success.
- Targeted tests passed:
- `pytest -q tests/tools/test_browser_windows_stream_port.py`

### WF-020 - Voice bootstrap self-heal + browser sidecar explicit-action priority
- Status: `done`
- Problem:
- `/voice` reported missing audio libs in active Windows runtime (`sounddevice` absent) with no built-in in-product remediation path.
- Browser sidecar turns could over-prioritize memory/worldview/file maintenance instead of executing explicit live browser action requests first.
- Code evidence:
- Added `/voice install` support in `cli.py`:
- `_handle_voice_command()` now accepts `install`.
- New `_install_voice_dependencies()` installer flow:
- prefers `uv pip install --python <current interpreter> sounddevice numpy` when `uv` exists.
- falls back to `<python> -m pip install sounddevice numpy`.
- includes `ensurepip --upgrade` recovery when pip launcher is missing.
- auto-runs `_enable_voice_mode()` after successful install.
- Added browser-sidecar intent override in injected context:
- `gateway/browser_bridge.py` now detects explicit live-action verbs in browser note text and emits instruction to execute requested browser actions first.
- Added guardrail that explicit live browser action should not be preempted by memory/worldview file work.
- Reinforced global memory guidance:
- `agent/prompt_builder.py` now explicitly says memory maintenance must not preempt explicit user action requests.
- Added tests:
- `tests/gateway/test_browser_bridge_context.py`
- `tests/tools/test_voice_cli_integration.py` (`/voice install` routing)
- File refs:
- `cli.py`
- `gateway/browser_bridge.py`
- `agent/prompt_builder.py`
- `tests/gateway/test_browser_bridge_context.py`
- `tests/tools/test_voice_cli_integration.py`
- Validation notes:
- Runtime install executed in active Hermes tool env:
- `uv pip install --python C:\Users\btgil\AppData\Roaming\uv\tools\hermes-agent\Scripts\python.exe sounddevice numpy` -> installed `sounddevice`.
- Post-install requirement check in active Hermes runtime returned:
- `env_available=True`, `req_available=True`, `missing=[]`.
- `python -m py_compile` passed for changed files (pre-existing warning in `cli.py` unchanged).
- Targeted tests passed:
- `3 passed` across browser-context + voice install routing tests.

### WF-021 - Terminal shell-awareness prompt injection (cmd/PowerShell/WSL)
- Status: `done`
- Problem:
- In native Windows terminal sessions, the model could misclassify shell context and take irrelevant file-memory actions instead of executing direct terminal requests using correct shell/path semantics.
- Code evidence:
- Added Windows shell environment helper to agent runtime:
- `AIAgent._build_environment_hint()` now emits shell-specific guidance for:
- `cmd` (cmd.exe commands + `%VAR%` + `C:\\` paths)
- `powershell` (PowerShell-native commands/path semantics)
- `wsl` (POSIX commands + `/mnt/<drive>/...` paths)
- Includes explicit action-priority guidance: direct terminal asks should be executed before memory/worldview file reads.
- Added per-call prompt injection wiring:
- `_handle_max_iterations()` now appends environment hint into ephemeral system additions.
- Main conversation API message assembly now appends environment hint into ephemeral system additions for every turn.
- Ported helper module from personal repo:
- `tools/environments/shell_utils.py` added for shell-mode detection (`HERMES_WINDOWS_SHELL`, auto mode, wsl/pwsh/cmd helpers).
- Added regression tests:
- `tests/test_run_agent.py` new `TestBuildEnvironmentHint` coverage for non-Windows, cmd, PowerShell, and WSL branches.
- File refs:
- `run_agent.py`
- `tools/environments/shell_utils.py`
- `tests/test_run_agent.py`
- Validation notes:
- `python -m py_compile run_agent.py tools/environments/shell_utils.py tests/test_run_agent.py` passed.
- Targeted tests passed:
- `pytest -q tests/test_run_agent.py -k BuildEnvironmentHint` -> `4 passed`.

### WF-022 - Windows shell override config fallback (env + config parity)
- Status: `done`
- Problem:
- Shell selection could still resolve to `wsl` when `HERMES_WINDOWS_SHELL=cmd` existed in `~/.hermes/config.yaml` but was not present in the live process environment.
- Code evidence:
- Added normalized Windows shell override resolver in `tools/environments/shell_utils.py`:
- `resolve_windows_shell_override()` now checks:
- process env (`HERMES_WINDOWS_SHELL`) first
- then `~/.hermes/config.yaml` keys (`HERMES_WINDOWS_SHELL`, `terminal.windows_shell`, `terminal.shell_mode`, `terminal.shell`)
- `get_local_shell_mode()` now consumes this resolver and logs source (`env`/`config`/`default`).
- Updated `run_agent.py` environment hint injection to use resolved override value (not raw env read).
- Added regression tests in `tests/test_run_agent.py`:
- env precedence over config
- config fallback when env missing
- hint uses resolved override path
- File refs:
- `tools/environments/shell_utils.py`
- `run_agent.py`
- `tests/test_run_agent.py`

### WF-023 - Local terminal backend now honors shell mode (cmd/PowerShell/WSL)
- Status: `done`
- Problem:
- `terminal` local backend one-shot execution was hardwired to bash via `tools/environments/local.py` (`_find_bash` + `bash -lic`), which could ignore Windows shell-mode intent and produce WSL/bash behavior.
- Code evidence:
- Updated `tools/environments/local.py` one-shot execution path:
- now uses `build_local_subprocess_invocation(...)` from `tools/environments/shell_utils.py`.
- keeps fence wrapping only for `posix`/`wsl` shell modes.
- uses sanitized subprocess env + platform-aware process tree termination (`terminate_process_tree`) for interrupt/timeout handling.
- preserved persistent-shell support (`PersistentShellMixin`) for existing behavior and tests.
- Added regression tests:
- `tests/tools/test_local_windows_shell_dispatch.py`
- covers cmd-mode dispatch without fence wrapping.
- covers wsl-mode dispatch with fence wrapping.
- Validation notes:
- `python -m py_compile tools/environments/local.py tests/tools/test_local_windows_shell_dispatch.py` passed.
- `pytest -q -o addopts='' tests/tools/test_local_windows_shell_dispatch.py` -> `2 passed`.

### WF-024 - Windows live-CDP browser stability + faster bind-failure turnaround
- Status: `done`
- Problem:
- `/browser connect` could succeed, but follow-up `browser_navigate` in Windows live-CDP mode still failed with daemon bind `10013`, and retries could feel stalled for too long.
- Root findings:
- `agent-browser --cdp 9222` reproduced bind `10013` on host.
- `agent-browser --session <name> --cdp 9222` succeeded on host.
- In Windows CDP mode, setting `AGENT_BROWSER_SOCKET_DIR` reproduced the bind failure; omitting it avoided the failure mode.
- Code evidence:
- `tools/browser_tool.py`:
- Windows CDP mode now includes explicit `--session <session_name>` alongside `--cdp <url>` for daemon startup stability.
- Windows CDP mode now skips `AGENT_BROWSER_SOCKET_DIR` override (uses agent-browser default socket dir).
- Existing bind-10013 recovery path retained and expanded (pid-file kill + taskkill fallback + retry).
- Retry attempts in bind-recovery path now use short timeout caps (`min(timeout, 10)`) to avoid minute-plus stalls.
- `browser_navigate()` command timeout reduced from `60s` to `30s` to keep UI responsiveness.
- Added regression tests:
- `tests/tools/test_browser_windows_stream_port.py`
- new coverage for Windows CDP session-arg behavior.
- new coverage that Windows CDP path does not set `AGENT_BROWSER_SOCKET_DIR`.
- Validation notes:
- `python -m py_compile tools/browser_tool.py` passed.
- `pytest -q -o addopts='' tests/tools/test_browser_windows_stream_port.py` -> `5 passed`.

### WF-025 - Graceful shutdown when browser cleanup is interrupted
- Status: `done`
- Problem:
- Exiting Hermes on Windows could show a full traceback when `Ctrl+C` hit during browser cleanup, because shutdown cleanup only caught `Exception` and `KeyboardInterrupt` escaped.
- Code evidence:
- `tools/browser_tool.py`:
- `_emergency_cleanup_all_sessions()` now catches `BaseException` and logs at debug level.
- `cleanup_browser()` close path now catches `BaseException` and uses shorter close timeout (`4s`) to reduce shutdown stalls.
- `cli.py`:
- `_run_cleanup()` now catches `BaseException` around terminal/browser/MCP shutdown steps to avoid noisy tracebacks during forced exit.
- Added regression test:
- `tests/tools/test_browser_windows_stream_port.py`
- `test_cleanup_browser_handles_keyboard_interrupt_during_close`
- Validation notes:
- `python -m py_compile tools/browser_tool.py cli.py tests/tools/test_browser_windows_stream_port.py` passed.
- `pytest -q -o addopts='' tests/tools/test_browser_windows_stream_port.py` -> `6 passed`.

### WF-026 - Terminal `hermes` navigation stall root-cause fix (import shadow + Windows capture deadlock)
- Status: `done`
- Problem:
- `hermes` in terminal kept repeating legacy behavior and browser navigation stalled/hung despite code patches in repo.
- Root cause:
- Launching from `C:\Users\btgil\.hermes` allowed local shadow modules (`cli.py`, `hermes_cli/`, `tools/`, etc.) to override package imports.
- Browser command execution on Windows used `capture_output=True`; with agent-browser daemon descendants, pipe handles could remain open and stall `subprocess.run(...).communicate(...)`.
- Additional compatibility issue:
- local CDP targets passed as `--cdp ws://localhost:9222` were less reliable than plain port form with current agent-browser behavior.
- Code evidence:
- `cli.py` + `run_agent.py`:
- added `_ensure_project_root_precedence()` so project root is forced to `sys.path[0]` before local imports.
- `tools/browser_tool.py`:
- added `_normalize_agent_browser_cdp_arg()` to convert localhost ws/wss CDP URLs to bare port (e.g., `9222`) for CLI invocation.
- replaced direct `subprocess.run(..., capture_output=True)` in browser command path with `_run_agent_browser_subprocess()`:
- uses file-based stdout/stderr capture on Windows to avoid pipe-EOF deadlocks.
- preserves parsed stdout/stderr behavior and timeout handling.
- Environment cleanup performed:
- moved conflicting shadow modules in `C:\Users\btgil\.hermes` into backup folder:
- `C:\Users\btgil\.hermes\_shadow_backup_20260316_172529`
- moved: `cli.py`, `agent/`, `cron/`, `gateway/`, `hermes_cli/`, `tools/`, `tests/`
- Added regression tests:
- `tests/tools/test_browser_windows_stream_port.py`
- `test_cdp_arg_normalization_localhost_ws_url_to_port`
- `test_cdp_arg_normalization_keeps_non_localhost_url`
- updated existing browser runner tests to mock new subprocess wrapper.
- Validation notes:
- `python -m py_compile tools/browser_tool.py tests/tools/test_browser_windows_stream_port.py run_agent.py cli.py` passed.
- `pytest -q -o addopts='' tests/tools/test_browser_windows_stream_port.py` -> `8 passed`.
- Live repro check (uv tool runtime, cwd `C:\Users\btgil\.hermes`) now succeeds:
- `_run_browser_command(... open https://github.com ...)` returned success in ~`1.16s`.

### WF-027 - Exit-time browser cleanup interrupt noise + Honcho SDK restore
- Status: `done`
- Problem:
- Closing Hermes after browser use could print noisy atexit traceback:
- `Exception ignored in atexit callback ... _stop_browser_cleanup_thread ... KeyboardInterrupt`
- Honcho initialization also warned:
- `Honcho init failed: honcho-ai is required for Honcho integration`
- Code evidence:
- `tools/browser_tool.py`
- `_stop_browser_cleanup_thread()` now catches `BaseException` around thread join and logs at debug level.
- Added regression test:
- `tests/tools/test_browser_windows_stream_port.py`
- `test_stop_browser_cleanup_thread_handles_join_interrupt`
- Runtime environment fix performed:
- Reinstalled Honcho SDK into active uv tool runtime:
- `uv pip install --python C:\Users\btgil\AppData\Roaming\uv\tools\hermes-agent\Scripts\python.exe "honcho-ai>=2.0.1"`
- Verified import in that runtime:
- `import honcho` succeeded (`honcho_import_ok`).
- Validation notes:
- `pytest -q -o addopts='' tests/tools/test_browser_windows_stream_port.py` -> `9 passed`.

### WF-028 - UV-first install guidance cleanup (voice + honcho + ACP)
- Status: `done`
- Problem:
- User-facing install hints still mixed in pip-first text in a uv-tool runtime, which made failures look inconsistent/noisy (`pip` missing warnings vs uv-managed install reality).
- Code evidence:
- `honcho_integration/cli.py`
- `_ensure_sdk_installed()` "skip install" guidance now prints uv-first command with current interpreter and pip fallback.
- `tools/voice_mode.py`
- environment warning now says `run /voice install` instead of pip-only wording.
- `AudioRecorder.start()` install error now prints:
- `/voice install` first
- uv pip command for current runtime when available
- python `-m pip` fallback.
- requirements details now avoid pip-first phrasing:
- `Audio capture: MISSING (run /voice install)`
- `STT provider: MISSING (install faster-whisper in Hermes runtime, ...)`
- `cli.py`
- voice-mode runtime errors now provide `/voice install` plus uv-first/fallback commands.
- `/voice on` unmet-requirements block now prints `/voice install` first, then uv/pip command lines.
- `hermes_cli/main.py`
- ACP ImportError guidance now prints uv-first editable install command and explicit pip fallback.
- Validation notes:
- `python -m py_compile honcho_integration/cli.py tools/voice_mode.py cli.py hermes_cli/main.py` passed.
- `pytest -q -o addopts='' tests/honcho_integration/test_cli.py` -> `5 passed`.
- `pytest -q -o addopts='' tests/tools/test_browser_windows_stream_port.py` -> `9 passed`.
- Known local test env issue (not introduced by this change):
- `tests/tools/test_voice_mode.py` currently fails to import due missing optional dependency `firecrawl` via `tools/__init__.py`.

### WF-029 - Voice runtime dependency restore + CLI `/voice` readiness verification
- Status: `done`
- Problem:
- Voice mode previously reported missing audio libs in Hermes runtime (`sounddevice` absent in uv tool interpreter).
- Changes performed:
- Installed `sounddevice` into active Hermes uv runtime:
- `uv pip install --python C:\Users\btgil\AppData\Roaming\uv\tools\hermes-agent\Scripts\python.exe sounddevice`
- Validation performed:
- Runtime package check:
- `uv pip list --python ...` shows `sounddevice 0.5.5`, `numpy 2.4.3`, `faster-whisper 1.2.1`.
- Direct runtime import/device probe:
- `import sounddevice` succeeded; `sd.query_devices()` returned `197` devices.
- Hermes CLI smoke:
- launched `hermes` and ran `/voice status`.
- observed:
- `Requirements:`
- `Audio capture: OK`
- `STT provider: OK (OpenAI)` (in this interactive run).

## Open Follow-ups
- Re-run targeted pytest in your preferred Windows environment after current merge work settles to confirm no hidden cross-fixture assumptions remain.
- Keep this ledger as source-of-truth for Windows stability fixes; append entries instead of rewriting history.
