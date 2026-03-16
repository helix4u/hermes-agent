# Personal Repo Integrations Ledger

Updated: 2026-03-16
Target repo: `c:\Users\btgil\.hermes\hermes-agent`
Reference repo: `c:\Users\btgil\.hermes\hermes-agent1`

This ledger tracks private integration work by **code comparison and runtime validation**, not commit/PR titles.

## Status Legend
- `done`: Integrated into `hermes-agent` and validated at least with compile/smoke checks.
- `in_progress`: Partially integrated; additional slices remain.
- `pending`: Not yet integrated from personal repo.
- `deferred`: Intentionally postponed due risk/scope.

## Entries

### PRI-001 - Browser sidecar bridge integration
- Status: `done`
- Scope:
- Added browser bridge server module and gateway `/inject` + `/session` integration.
- Added sidecar toolsets and sidecar async session/progress plumbing.
- Added bridge-side TTS/transcription endpoints and media URL serving support.
- File refs:
- `gateway/browser_bridge.py`
- `gateway/run.py`
- `toolsets.py`
- Validation:
- `py_compile` passed for changed files.
- Direct venv smoke checks covered sidecar `state/list/reset/interrupt/tts`.

### PRI-002 - F5 TTS runtime integration
- Status: `done`
- Integrated slices:
- Added F5 provider runtime to `tools/tts_tool.py`:
- `DEFAULT_F5_*` constants, JWT token minting, text chunking, WAV concatenation, and `_generate_f5_tts()`.
- Added preflight key hardening for F5 JWT signing:
- `F5TTS_SECRET_KEY` now requires at least 32 bytes before token minting (clear error instead of downstream PyJWT key-length warning).
- Provider routing now supports `provider == "f5"`.
- Auto output path uses `.wav` for F5, with Telegram Opus conversion path shared with Edge.
- OpenAI voice key resolution now accepts `VOICE_TOOLS_OPENAI_KEY` or `OPENAI_API_KEY`.
- Added F5 config/env wiring to `hermes_cli/config.py`:
- `DEFAULT_CONFIG["tts"]["f5"]` block.
- `OPTIONAL_ENV_VARS["F5TTS_SECRET_KEY"]`.
- `show_config()` API key display includes F5.
- `set_config_value()` `.env` passthrough list includes F5 key.
- Config migration metadata updated: `_config_version: 9`, `ENV_VARS_BY_VERSION[9]`.
- Added user-facing F5 docs to `cli-config.yaml.example`:
- commented `tts.f5` snippet and `.env` key example.
- documented 32+ byte minimum for `F5TTS_SECRET_KEY` with PowerShell sample key generation command.
- updated TTS toolset descriptions to include local F5.
- Skill assets synced:
- `skills/integrations/f5-tts/SKILL.md`
- `skills/integrations/f5-tts/references/api.md`
- Validation:
- `python -m py_compile tools/tts_tool.py hermes_cli/config.py` passed.
- Targeted grep confirms F5 runtime/config/docs markers are present.
- Remaining gap:
- Full live synthesis smoke test against a running local F5 service endpoint is still recommended.

### PRI-003 - OBS scene-capture skill
- Status: `done`
- Synced into target repo:
- `SKILL.md`
- `references/obs-websocket.md`
- `scripts/obs_still.py`

### PRI-004 - Plex playlist skills
- Status: `done`
- Synced into target repo:
- `SKILL.md`
- `scripts/plex_export_library.py`
- `scripts/plex_make_playlists.py`
- `scripts/plex_movie_playlist.py`
- `scripts/plex_music_playlist.py`

### PRI-005 - Discord terminal switching + embed response behavior
- Status: `done`
- Integrated slices:
- `gateway/platforms/discord.py` now sends regular assistant responses as chained embeds (instead of plain message content), with bounded chunk size, retry pacing, and system-message reply fallback.
- `gateway/platforms/discord.py` `edit_message()` now edits embed descriptions to match embed-based send behavior.
- Added Discord slash command `/terminal` that dispatches `/terminal <mode>` into the existing gateway command handler.
- Added Discord "Listen" button UX for assistant embeds:
- `ListenButtonView` and `PersistentListenButtonView` components.
- `_handle_discord_listen()` TTS flow: reads embed text, calls `text_to_speech_tool`, replies with `send_voice`.
- Registered persistent listen view at Discord ready-time for custom-id callback handling.
- Added progress-safe behavior:
- `gateway/run.py` tool-progress metadata now includes `tool_progress: true`.
- `gateway/platforms/discord.py` suppresses listen button rendering for progress messages.
- Added message-delivery hardening and moderation controls:
- duplicate Discord message-ID suppression window (`_seen_message_ids`) to avoid repeated processing.
- `on_raw_reaction_add` handler allowing users to delete bot messages via ❌/x/✖ reactions.
- Added Discord slash-command parity improvements:
- Added `/cron` slash group with `list`, `add`, `remove`, and `run` subcommands routed through existing `/cron ...` handlers.
- Updated slash completion helper to support `delete_original_response` behavior for low-noise ephemeral command acks.
- `/terminal` slash now uses delete-original completion instead of extra followup text.
- Added Discord send observability parity:
- embed chunk send retry path now logs attempt number, chunk index, payload length, HTTP status/code, and retry-after details for API troubleshooting.
- Added Discord startup/connect resilience parity from personal repo:
- Split non-critical startup into `_run_post_ready_startup()` so gateway readiness is not blocked by slash sync / username resolution.
- Added `_attempt_connect()` lifecycle flow with explicit `start_task` tracking and clean shutdown-on-timeout.
- Added privileged-intents fallback retry path (`message_content=False`, `members=False`) when Discord rejects privileged intents.
- Added explicit `_client_task` / `_post_ready_task` cancellation handling in `disconnect()` to avoid leaked background tasks.
- Restored terminal switching command handling in gateway runtime:
- Added `_handle_terminal_command()` in `gateway/run.py`.
- Added `/terminal` + `/shell` command recognition/dispatch in command routing.
- Added out-of-band `/terminal` handling while a session is actively running (no forced interrupt required).
- Validation:
- `python -m py_compile gateway/platforms/discord.py` passed.
- `rg` verification confirms `MAX_EMBED_DESCRIPTION`, chain send settings, and `slash_terminal` registration are present.
- `python -m py_compile gateway/platforms/discord.py gateway/run.py` passed.
- re-run `python -m py_compile gateway/platforms/discord.py` passed after dedup/reaction patch.
- re-run `python -m py_compile gateway/platforms/discord.py` passed after chunk-send diagnostics patch.
- re-run `python -m py_compile gateway/platforms/discord.py` passed after connect/disconnect startup resilience merge.
- `python -m py_compile gateway/run.py` passed after `/terminal` handler restore.
- Notes:
- Core scope for this item is now integrated (embed response behavior + terminal switching + related reliability slices).

### PRI-006 - Private skill set parity sweep
- Status: `done`
- Goal:
- Compare and sync additional personal skills beyond F5/OBS/Plex using directory-level and file-level diffs.
- Approach:
- Prefer additive skill directory sync first (low risk), then runtime wiring changes.
- Additional skills synced this batch:
- `skills/integrations/bird/SKILL.md`
- `skills/integrations/gemini/SKILL.md`
- `skills/integrations/weather/SKILL.md`
- `skills/media/yt-dlp/SKILL.md`
- `skills/media/yt-dlp/scripts/fetch_transcript.py`
- Parity sweep check:
- Skill-name presence check (`SKILL.md` directory leaf names) now reports no missing personal skill names in target.
- Validation:
- `python -m py_compile skills/media/yt-dlp/scripts/fetch_transcript.py` passed.
- Directory presence verified in target repo after copy.

### PRI-007 - Windows gateway lifecycle parity (`hermes_cli/gateway.py`)
- Status: `done`
- Integrated slices:
- Upgraded Windows gateway process discovery:
- `find_gateway_pids()` now prefers PID-file verification via `gateway.status.get_running_pid()` and falls back to PowerShell CIM/WMI process scanning (instead of WMIC-only parsing).
- Upgraded Windows process termination semantics:
- `kill_gateway_processes()` now uses `taskkill /PID <id> /T` (and `/F` when forced) for process-tree cleanup.
- Added Windows detached gateway lifecycle helpers:
- `get_windows_hermes_command()`
- `reset_gateway_logs()`
- `windows_start_detached_gateway()`
- `windows_stop_gateway()`
- `windows_gateway_status()`
- Added richer runtime summary helper:
- `print_runtime_health_summary()` prints persisted gateway/platform health states from runtime status.
- Wired command dispatch for Windows:
- `gateway start|stop|restart|status` now routes to dedicated Windows handlers in `gateway_command()`.
- Validation:
- `python -m py_compile hermes_cli/gateway.py` passed.
- `python -m py_compile gateway/platforms/discord.py hermes_cli/gateway.py` passed as a combined check.
- re-run `python -m py_compile hermes_cli/gateway.py gateway/run.py` passed after detached-start log reset wiring.

### PRI-008 - Browser sidecar policy/config parity in gateway runtime
- Status: `done`
- Integrated slices:
- Added `browser_sidecar` config loading in `gateway/run.py` (alongside `platform_toolsets`).
- Browser-sidecar sessions now support config-driven policy:
- `browser_sidecar.toolsets` for explicit sidecar toolset override.
- `browser_sidecar.allow_delegation` for delegating sidecar preset selection when no explicit toolsets are provided.
- `browser_sidecar.max_turns` for sidecar-specific iteration budget override.
- Retained existing env-based fallback behavior for delegation (`HERMES_BROWSER_SIDECAR_ALLOW_DELEGATION`) when config is unset.
- Added browser config-to-env bridge in `gateway/run.py`:
- `browser.backend`, `inactivity_timeout`, `navigate_timeout`, `headless`, `profile_dir`, `user_agent` now map into corresponding `BROWSER_*` env vars.
- Updated `cli-config.yaml.example` docs:
- Expanded `browser:` section with commented backend/navigation/headless/profile/user-agent options.
- Added new commented `browser_sidecar:` section documenting `toolsets`, `allow_delegation`, and `max_turns`.
- Validation:
- `python -m py_compile gateway/run.py` passed.
- `python -m py_compile gateway/run.py gateway/platforms/discord.py hermes_cli/gateway.py` passed.

### PRI-009 - Gateway log command parity (`hermes gateway logs`)
- Status: `done`
- Integrated slices:
- Added gateway log utilities to `hermes_cli/gateway.py`:
- `get_gateway_log_paths()`
- `read_recent_gateway_logs(lines, include_error)`
- `follow_gateway_logs(lines, include_error)`
- `show_gateway_logs(lines, follow, include_error)`
- Added new `gateway_command()` branch for `subcmd == "logs"` in `hermes_cli/gateway.py`.
- Added CLI parser support in `hermes_cli/main.py`:
- `hermes gateway logs`
- `--lines/-n`
- `--follow/-f`
- `--error` (include `gateway-error.log`)
- Validation:
- `python -m py_compile hermes_cli/gateway.py hermes_cli/main.py` passed.
- `python -m hermes_cli.main gateway logs --help` passed.
- `python -m hermes_cli.main gateway logs -n 2` returned recent log lines.

### PRI-010 - Sidecar soft tool-call budget parity
- Status: `done`
- Integrated slices:
- Added `max_tool_calls_per_run` support to `AIAgent` in `run_agent.py`:
- New constructor argument and state tracking (`self.max_tool_calls_per_run`).
- Per-run executed-tool counter (`self._tool_calls_executed_total`) with reset at conversation start.
- Tool-call counter increments in both tool execution paths.
- Added transient `_append_tool_budget_guidance()` API-message injection to inform the model of:
- iterations remaining
- tool calls used
- tool calls remaining in the soft budget
- Wired sidecar policy setting through gateway runtime:
- `gateway/run.py` now reads `browser_sidecar.max_tool_calls` from config and passes it as `max_tool_calls_per_run` when creating `AIAgent`.
- Updated `cli-config.yaml.example` sidecar docs:
- added commented `browser_sidecar.max_tool_calls` entry.
- Validation:
- `python -m py_compile run_agent.py gateway/run.py` passed.
- `rg` verification confirms `max_tool_calls_per_run` appears in constructor, guidance logic, and gateway-side sidecar wiring.
- Note:
- Direct runtime import smoke (`python -c "import run_agent"`) in this environment failed due missing optional dependency `fire`; compile and marker checks were used instead.

### PRI-011 - InvokeAI defaults and wiki-host command parity (`gateway/run.py`)
- Status: `done`
- Integrated slices:
- Added persisted image-default helpers and command flow parity:
- `_get_image_generation_defaults()`
- `_save_image_generation_defaults()`
- `_format_image_defaults()`
- `/invokeai-defaults` and `/invokeai_defaults` command dispatch + `_handle_image_defaults_command()`.
- Added wiki LAN-host helpers and lifecycle parity:
- `_build_wiki_web_root()`/`_cleanup_wiki_web_root()`
- `_get_wiki_host_config()`/`_save_wiki_host_config()`
- `_resolve_lan_ip()`, `_QuietWikiRequestHandler`, `_ExclusiveThreadingHTTPServer`
- `_wiki_host_status_message()`, `_start_wiki_host()`, `_stop_wiki_host()`
- Moved generated `wiki_serve` staging root outside workspace to `~/.hermes/.runtime/wiki_serve` so agents do not edit transient serve files.
- `/wiki-host` and `/wiki_host` command dispatch + `_handle_wiki_host_command()`.
- Added startup/shutdown restoration behavior:
- Gateway `start()` restores persisted wiki-host state when `wiki_hosting.enabled` is true.
- Gateway `stop()` now shuts down wiki host cleanly.
- Updated command discoverability:
- `/help` now documents `/invokeai-defaults` and `/wiki-host`.
- Validation:
- `python -m py_compile gateway/run.py` passed.
- `rg` verification confirms command aliases, handlers, and wiki/image helper symbols are present.
- Additional hardening folded in:
- Added top-level `import shutil` in `gateway/run.py` to support shared wiki-host helper file operations (`copytree`/`copy2`/`rmtree`) without local import duplication.

### PRI-012 - Discord cron/send_message embed-delivery parity
- Status: `done`
- Integrated slices:
- Updated Discord REST sender in `tools/send_message_tool.py` to send text as embed descriptions instead of plain `content`.
- Increased chunking limit from plain-message `2000` to embed-description `4000` characters per chunk.
- Added explicit empty-message guard for Discord REST sends.
- Result:
- Cron auto-delivery messages (including heartbeat-style cron posts) now follow embed-description delivery instead of raw content limits.
- File refs:
- `tools/send_message_tool.py`
- Validation:
- `python -m py_compile tools/send_message_tool.py` passed.

### PRI-013 - Discord voice transcript echo embed-delivery parity
- Status: `done`
- Integrated slices:
- Updated gateway voice-input transcript echo in `gateway/run.py` to use `adapter.send(...)` instead of direct `channel.send(...)`.
- Transcript echoes now flow through Discord adapter embed chunking behavior (same family of path as regular responses).
- Disabled listen-button attachment for transcript echoes (`include_listen_button=False`) to avoid turning transcript mirrors into TTS action prompts.
- Retained mention sanitization for `@everyone`/`@here`.
- File refs:
- `gateway/run.py`
- Validation:
- `python -m py_compile gateway/run.py` passed.

### PRI-014 - Discord slash `/cron` command routing + startup registration visibility
- Status: `done`
- Integrated slices:
- Added native gateway `/cron` command handling in `gateway/run.py` so `/cron ...` is handled as a first-class command instead of falling through into agent freeform responses.
- Supported gateway-side `/cron` subcommands: `list`, `add/create`, `remove/rm/delete`, `run`, `pause`, `resume`, `status`, `tick`.
- Added `/cron` to command recognition/hooks and priority command handling while a session is actively running.
- Updated `/help` command output to include `/cron` usage.
- Added explicit Discord startup log line after slash command registration (`Registered <n> slash command roots locally`) to restore visibility during gateway start.
- Added explicit startup console printout in `gateway/platforms/discord.py` after slash sync:
- one line with synced count and one line enumerating available slash commands (including grouped subcommands such as `/cron list`) so command availability is visible directly in the gateway window under startup output.
- File refs:
- `gateway/run.py`
- `gateway/platforms/discord.py`
- Validation:
- `python -m py_compile gateway/run.py gateway/platforms/discord.py` passed.

### PRI-015 - Windows `gateway start` inline startup feedback parity
- Status: `done`
- Integrated slices:
- Added inline startup log streaming in `hermes_cli/gateway.py` so Windows detached start/restart shows live startup output in the invoking terminal.
- Added `--no-startup-stream` flag in `hermes_cli/main.py` for `gateway start` and `gateway restart` to disable inline streaming when needed.
- Updated Windows start/restart dispatch wiring in `gateway_command()` to honor the new flag.
- File refs:
- `hermes_cli/gateway.py`
- `hermes_cli/main.py`
- Validation:
- `python -m py_compile hermes_cli/gateway.py hermes_cli/main.py` passed.

### PRI-016 - Windows startup feedback UX tuning (compact summary, not full logs)
- Status: `done`
- Integrated slices:
- Tuned `stream_gateway_startup_logs()` in `hermes_cli/gateway.py` to print a concise startup summary instead of full raw log lines.
- Keeps surfaced warning/error lines under an `Issues` subsection while avoiding startup spam.
- Added duplicate issue-line suppression in startup summary output.
- Reduced default startup watch duration to 8s for faster return to prompt.
- File refs:
- `hermes_cli/gateway.py`
- Validation:
- `python -m py_compile hermes_cli/gateway.py` passed.

### PRI-017 - Discord rolling tool-progress command window parity
- Status: `done`
- Integrated slices:
- Replaced simple tool-progress accumulation in `gateway/run.py` with a rolling progress window model:
- Added progress style support (`single` edit-in-place vs `feed`) via `display.tool_progress_style` and `HERMES_TOOL_PROGRESS_STYLE`.
- Added rolling event window sizing via `display.tool_progress_rolling_entries` and `HERMES_TOOL_PROGRESS_ROLLING_ENTRIES`.
- Added embed-safe render cap via `HERMES_TOOL_PROGRESS_EMBED_MAX_CHARS`.
- Added phase-aware progress header (`thinking`, `running commands`, `finalizing`) and rolling recent command events before final response delivery.
- Added `_tool_result` completion handling in progress callback, including duration/error outcome rendering.
- Documented the new progress-window tuning keys in `cli-config.yaml.example`.
- Added missing completion metadata parity in `run_agent.py` callbacks:
- Sequential and concurrent tool execution now emit `status_suffix` alongside `_tool_result` payloads, so progress updates can display richer command completion state.
- File refs:
- `gateway/run.py`
- `run_agent.py`
- `cli-config.yaml.example`
- Validation:
- `python -m py_compile run_agent.py gateway/run.py gateway/platforms/discord.py` passed.

### PRI-018 - Browser bridge lifecycle hardening (shutdown/interrupt races)
- Status: `done`
- Integrated slices:
- Hardened browser bridge request scheduling in `gateway/browser_bridge.py`:
- `run_payload()` now handles awaitable handlers safely, verifies loop availability, closes unscheduled coroutines on scheduling failure, and cancels pending futures on error/timeout paths.
- Browser bridge HTTP route now returns `503` on loop-unavailable runtime races instead of logging a full handler exception.
- Reduced noisy shutdown failure surface in gateway runtime:
- `start_gateway()` now catches cancellation around shutdown wait and attempts graceful stop before cleanup.
- Added clean Ctrl+C handling for foreground gateway run:
- `hermes_cli/gateway.py::run_gateway()` now catches `KeyboardInterrupt` and exits without large traceback output.
- File refs:
- `gateway/browser_bridge.py`
- `gateway/run.py`
- `hermes_cli/gateway.py`
- Validation:
- `python -m py_compile gateway/browser_bridge.py gateway/run.py hermes_cli/gateway.py` passed.

### PRI-019 - STT provider-selection guardrails + OpenAI pinning
- Status: `done`
- Integrated slices:
- Updated `tools/transcription_tools.py` provider selection behavior:
- Added model-based provider inference when `stt.provider` is omitted (prevents silent mismatch when config has API model names like `whisper-1`).
- Enforced explicit provider intent for OpenAI (`stt.provider: openai`) so provider resolution does not silently downgrade to local STT.
- Applied runtime config pin to OpenAI STT in user config (`~/.hermes/config.yaml`) for immediate voice-capture stability.
- File refs:
- `tools/transcription_tools.py`
- `C:\Users\btgil\.hermes\config.yaml`
- Validation:
- `python -m py_compile tools/transcription_tools.py` passed.

### PRI-020 - Browser tool Windows execution parity (`agent-browser`/`npx` launch path)
- Status: `done`
- Integrated slices:
- Hardened browser launcher resolution in `tools/browser_tool.py` for Windows:
- `_find_agent_browser()` now returns command-part lists (not split-sensitive strings).
- Added local `node_modules/.bin/agent-browser.cmd` detection.
- `npx` fallback now uses absolute path from `shutil.which("npx")` (`npx.CMD` on Windows) plus `"agent-browser"` arg.
- Hardened PATH augmentation in `_run_browser_command()`:
- replaced hardcoded `:` split/join with `os.pathsep`.
- normalized PATH dedupe to avoid duplicate/variant entries.
- Result:
- Eliminates Windows `[WinError 2]` failure mode seen during browser tool startup when only `npx` fallback is available.
- File refs:
- `tools/browser_tool.py`
- Validation:
- `python -m py_compile tools/browser_tool.py` passed.
- Direct probe in repo venv confirmed resolved command executes:
- `_find_agent_browser()` returned `['C:\\Program Files\\nodejs\\npx.CMD', 'agent-browser']`.
- `subprocess.run(parts + ['--version'])` returned rc `0` and printed `agent-browser 0.20.13`.

### PRI-021 - CI stabilization batch (deploy checkout auth + compatibility regressions)
- Status: `done`
- Integrated slices:
- Fixed GitHub Pages deploy checkout auth failure:
- Added `permissions.contents: read` in `.github/workflows/deploy-site.yml` so `actions/checkout` can fetch private repo contents.
- Added Discord compatibility guardrails for lightweight test stubs:
- Listen button style now falls back to `ButtonStyle.primary` when `secondary` is unavailable.
- `discord.ui.View` init in button views now tolerates stubbed `object` base classes.
- Cron slash-command group registration now gracefully skips `app_commands.Group` when unavailable in mocks.
- Hardened gateway shutdown behavior for partially initialized runners (common in unit tests):
- `GatewayRunner.stop()` now uses `getattr(...)` guards for browser-bridge state.
- `_stop_wiki_host()` now uses `getattr(...)` guards for wiki host attributes.
- Reduced tool-progress startup noise and restored topic test compatibility:
- progress sender no longer emits heartbeat-only messages before first real progress update.
- Restored backward-compatible tool-budget guidance defaults:
- `_append_tool_budget_guidance()` now defaults to `auto` mode and only injects guidance when a soft cap is explicitly configured (or forced by env).
- Refined STT provider fallback compatibility:
- OpenAI fallback order restored to `local -> groq -> local_command -> none` when OpenAI key is unavailable.
- Added optional strict provider pin (`stt.strict_provider` / `HERMES_STT_STRICT_PROVIDER`) to preserve explicit-pin behavior when desired.
- Updated targeted tests for intentional behavior changes (embed/progress/STT-env resilience):
- `tests/gateway/test_discord_send.py`
- `tests/gateway/test_run_progress_topics.py`
- `tests/gateway/test_voice_command.py`
- `tests/tools/test_transcription.py`
- `tests/tools/test_transcription_tools.py`
- File refs:
- `.github/workflows/deploy-site.yml`
- `gateway/platforms/discord.py`
- `gateway/run.py`
- `run_agent.py`
- `tools/transcription_tools.py`
- `tests/gateway/test_discord_send.py`
- `tests/gateway/test_run_progress_topics.py`
- `tests/gateway/test_voice_command.py`
- `tests/tools/test_transcription.py`
- `tests/tools/test_transcription_tools.py`
- Validation:
- `python -m py_compile gateway/platforms/discord.py gateway/run.py run_agent.py tools/transcription_tools.py tools/browser_tool.py` passed.
- Targeted regression suite passed:
- `11 passed, 294 deselected` across previously failing CI slices.

### PRI-022 - Deploy workflow Pages-preflight + fork-safe CNAME handling
- Status: `done`
- Integrated slices:
- Hardened `.github/workflows/deploy-site.yml` to avoid hard failure when GitHub Pages is not enabled in fork repos.
- Added preflight step that checks `GET /repos/{owner}/{repo}/pages` using `github.token`.
- Deploy steps now run only when Pages is enabled; otherwise the job exits cleanly with a notice.
- Made CNAME emission fork-safe:
- `hermes-agent.nousresearch.com` CNAME is now written only for upstream repo `NousResearch/hermes-agent`.
- Result:
- `Deploy Site` no longer fails with `404 Failed to create deployment` just because Pages is disabled on a fork.
- File refs:
- `.github/workflows/deploy-site.yml`
- Validation:
- Workflow syntax inspected after patch; step gating and skip-note behavior are explicit.

### PRI-023 - Browser sidecar fatal-failure path hardening (gateway stays up)
- Status: `done`
- Integrated slices:
- Hardened sidecar turn execution in `gateway/run.py`:
- Added explicit `CancelledError` handling in `_handle_browser_bridge_send()` turn runner to record interrupted state cleanly.
- Added `BaseException` handling in sidecar turn runner so fatal worker/tool exceptions become turn failures instead of gateway-killing errors.
- Hardened general message turn safety in `gateway/run.py`:
- Added `BaseException` guard in `_handle_message()` with a safe user-facing abort response while keeping gateway alive.
- Hardened shutdown cancellation resilience in `start_gateway()`:
- cancellation during active sidecar turns is now treated as unexpected and resumed (via task uncancel) unless shutdown was explicitly requested.
- Hardened bridge HTTP request failure behavior in `gateway/browser_bridge.py`:
- `do_POST()` now catches `BaseException` and returns controlled 500 responses for fatal request handler crashes.
- File refs:
- `gateway/run.py`
- `gateway/browser_bridge.py`
- Validation:
- `python -m py_compile gateway/run.py gateway/browser_bridge.py hermes_cli/gateway.py` passed.

### PRI-024 - CLI browser-connect Windows launch reliability + exit flush crash containment
- Status: `done`
- Integrated slices:
- Hardened Windows `/browser connect` launch path in `cli.py`:
- Added explicit Windows Chrome/Edge executable discovery (Program Files, Program Files x86, LOCALAPPDATA, PATH).
- Launch now includes deterministic remote-debug args and isolated debug profile dir (`--user-data-dir=%TEMP%\\chrome-cdp-hermes`).
- Windows launch now uses detached process flags for cleaner CLI behavior.
- Improved manual fallback command text on Windows:
- now prints fully-qualified executable invocation instead of relying on `chrome.exe` being on PATH.
- Added localhost CDP reachability guard:
- `/browser connect` no longer declares connected when endpoint is unreachable.
- Hardened CLI memory flush on session rollover/exit:
- `new_session()` and CLI shutdown finalizer now catch `BaseException` around `flush_memories(...)` to prevent exit-time crash traces.
- File refs:
- `cli.py`
- Validation:
- `python -m py_compile cli.py` passed.

### PRI-025 - Browser tool daemon bind failure (`agent-browser` Windows 10013)
- Status: `done`
- Integrated slices:
- Added deterministic Windows bind-failure handling in `tools/browser_tool.py`:
- if unset, force `AGENT_BROWSER_STREAM_PORT=0` for `agent-browser` invocations.
- detect daemon bind signature (`Failed to bind TCP` + WinError `10013`/permissions text).
- auto-retry the browser command with a safe stream-port override.
- Added regression tests:
- `tests/tools/test_browser_windows_stream_port.py`
- Validation:
- Repro before patch (host command):
- `agent-browser --session hermes_bindprobe --json open https://example.com` -> daemon bind `10013`.
- Repro with override:
- `set AGENT_BROWSER_STREAM_PORT=0 && agent-browser --session hermes_bindprobe2 --json open https://example.com` -> success.
- Targeted tests:
- `pytest -q tests/tools/test_browser_windows_stream_port.py`

### PRI-026 - Voice install command + sidecar explicit browser-action execution priority
- Status: `done`
- Integrated slices:
- Added in-product voice dependency bootstrap in `cli.py`:
- `/voice install` subcommand routing in `_handle_voice_command()`.
- new `_install_voice_dependencies()` helper installs `sounddevice`/`numpy` into the active Hermes runtime with resilient flow (`uv pip --python`, pip fallback, ensurepip recovery).
- successful install auto-enables voice mode (`_enable_voice_mode()`).
- Improved browser sidecar action priority in injected context:
- `gateway/browser_bridge.py` now detects explicit live-action phrases in user note (`open`, `navigate`, `click`, etc.).
- injected instructions now explicitly direct the model to execute requested browser actions first and not preempt with memory/worldview file reads.
- Reinforced base memory guidance:
- `agent/prompt_builder.py` now states memory upkeep must not preempt explicit direct user actions.
- Added regression tests:
- `tests/gateway/test_browser_bridge_context.py` (explicit-live-action vs reference-only context behavior).
- `tests/tools/test_voice_cli_integration.py` (`/voice install` command routing).
- File refs:
- `cli.py`
- `gateway/browser_bridge.py`
- `agent/prompt_builder.py`
- `tests/gateway/test_browser_bridge_context.py`
- `tests/tools/test_voice_cli_integration.py`
- Validation:
- Runtime install performed in active Hermes tool runtime:
- `uv pip install --python C:\Users\btgil\AppData\Roaming\uv\tools\hermes-agent\Scripts\python.exe sounddevice numpy` -> installed `sounddevice`.
- Runtime check in Hermes tool interpreter:
- `detect_audio_environment().available == True`
- `check_voice_requirements().available == True`
- `missing_packages == []`
- `python -m py_compile` passed for updated files.
- Targeted pytest passed:
- `3 passed` for browser-context + `/voice install` slices.

### PRI-027 - OS-helper system prompt injection parity for terminal sessions
- Status: `done`
- Integrated slices:
- Ported shell-aware environment hint behavior into `run_agent.py`:
- Added `AIAgent._build_environment_hint()` with explicit branches for `cmd`, `powershell`, and `wsl`.
- Hint text now includes shell/path semantics and explicit direct-action priority to reduce “read memory/worldview first” drift in terminal tasks.
- Wired environment hint into per-call ephemeral prompt path:
- main conversation API message assembly.
- max-iteration summary fallback API message assembly.
- Added helper module from personal repo:
- `tools/environments/shell_utils.py` for shell-mode detection helpers and `HERMES_WINDOWS_SHELL` awareness.
- Added regression coverage:
- `tests/test_run_agent.py` includes `TestBuildEnvironmentHint` with cmd/PowerShell/WSL + non-Windows cases.
- File refs:
- `run_agent.py`
- `tools/environments/shell_utils.py`
- `tests/test_run_agent.py`
- Validation:
- `python -m py_compile run_agent.py tools/environments/shell_utils.py tests/test_run_agent.py` passed.
- `pytest -q tests/test_run_agent.py -k BuildEnvironmentHint` passed (`4 passed`).

### PRI-028 - Shell override source parity (env + config.yaml)
- Status: `done`
- Integrated slices:
- Extended shell override resolution in `tools/environments/shell_utils.py`:
- Added `resolve_windows_shell_override()` with precedence:
- `HERMES_WINDOWS_SHELL` from process env
- fallback to `~/.hermes/config.yaml` (`HERMES_WINDOWS_SHELL`, plus terminal shell aliases)
- normalization for `cmd.exe`/`pwsh` aliases.
- `get_local_shell_mode()` now uses the shared resolver and logs selected override source (`env`, `config`, `default`).
- Updated environment hint path in `run_agent.py`:
- `_build_environment_hint()` now uses resolved override value from shell utils, avoiding stale direct-env display mismatch.
- Added tests in `tests/test_run_agent.py` for:
- env precedence over config
- config fallback when env is missing
- hint text using resolved override path
- File refs:
- `tools/environments/shell_utils.py`
- `run_agent.py`
- `tests/test_run_agent.py`

### PRI-029 - Local terminal execution dispatch parity (shell_utils integration)
- Status: `done`
- Integrated slices:
- Ported shell-aware one-shot dispatch behavior into `tools/environments/local.py`:
- replaced bash-only one-shot invocation with `build_local_subprocess_invocation(...)`.
- shell fences remain enabled only for `posix`/`wsl` modes to keep startup noise filtering behavior.
- reused platform-aware termination helper (`terminate_process_tree`) in interrupt/timeout handling.
- preserved `PersistentShellMixin` path and local persistent shell support.
- Added regression tests:
- `tests/tools/test_local_windows_shell_dispatch.py` for cmd vs wsl dispatch behavior.
- File refs:
- `tools/environments/local.py`
- `tests/tools/test_local_windows_shell_dispatch.py`
- Validation:
- `python -m py_compile tools/environments/local.py tests/tools/test_local_windows_shell_dispatch.py` passed.
- `pytest -q -o addopts='' tests/tools/test_local_windows_shell_dispatch.py` passed (`2 passed`).

### PRI-030 - Windows live-CDP browser bridge stabilization + latency guardrails
- Status: `done`
- Integrated slices:
- Hardened Windows CDP command construction in `tools/browser_tool.py`:
- when `cdp_url` is active on Windows, invoke `agent-browser` with `--session <session_name> --cdp <url>` for startup stability.
- added Windows+CDP compatibility mode that skips `AGENT_BROWSER_SOCKET_DIR` env injection (uses default agent-browser socket dir).
- Extended bind-10013 recovery behavior:
- retains stream-port retries plus stale-daemon cleanup (`pid` kill + `taskkill` image fallback).
- Retry responsiveness improvements:
- bind-recovery retries now use capped short timeout (`min(timeout, 10)`).
- `browser_navigate()` timeout reduced from 60s to 30s to avoid long perceived hangs.
- Added regression tests:
- `tests/tools/test_browser_windows_stream_port.py`
- `test_windows_cdp_mode_includes_session_arg_for_stability`
- `test_windows_cdp_mode_skips_custom_socket_dir_env`
- File refs:
- `tools/browser_tool.py`
- `tests/tools/test_browser_windows_stream_port.py`
- Validation:
- `python -m py_compile tools/browser_tool.py tests/tools/test_browser_windows_stream_port.py` passed.
- `pytest -q -o addopts='' tests/tools/test_browser_windows_stream_port.py` passed (`5 passed`).

### PRI-031 - Shutdown cleanup resilience (interrupt-safe browser/session teardown)
- Status: `done`
- Integrated slices:
- Hardened shutdown cleanup exception boundaries:
- `cli.py` `_run_cleanup()` now catches `BaseException` across terminal/browser/MCP cleanup steps.
- Browser cleanup path now handles interrupt conditions without traceback spam:
- `tools/browser_tool.py` `_emergency_cleanup_all_sessions()` catches `BaseException`.
- `cleanup_browser()` now catches `BaseException` around `close` command and uses shorter close timeout (`4s`) for faster exit.
- Added regression test:
- `tests/tools/test_browser_windows_stream_port.py`
- `test_cleanup_browser_handles_keyboard_interrupt_during_close`
- File refs:
- `cli.py`
- `tools/browser_tool.py`
- `tests/tools/test_browser_windows_stream_port.py`
- Validation:
- `python -m py_compile tools/browser_tool.py cli.py tests/tools/test_browser_windows_stream_port.py` passed.
- `pytest -q -o addopts='' tests/tools/test_browser_windows_stream_port.py` passed (`6 passed`).

### PRI-032 - Deterministic terminal runtime imports + Windows browser command unstick
- Status: `done`
- Integrated slices:
- Added project-root precedence guards:
- `cli.py` and `run_agent.py` now force their own project root to `sys.path[0]` before local imports to prevent cwd shadowing by sibling modules.
- Hardened browser CDP argument handling:
- `tools/browser_tool.py` adds `_normalize_agent_browser_cdp_arg()` to convert localhost ws/wss CDP URLs to plain port for agent-browser invocation.
- Replaced Windows browser subprocess pipe capture:
- new `_run_agent_browser_subprocess()` uses file-based stdout/stderr capture on Windows, avoiding hangs caused by inherited pipe handles from daemon descendants.
- Integrated into `_run_browser_command()` for primary and retry paths.
- Updated regression suite:
- `tests/tools/test_browser_windows_stream_port.py` now mocks `_run_agent_browser_subprocess`.
- added CDP normalization tests for localhost and remote endpoints.
- Environment cleanup executed to remove import-shadow collisions from `C:\Users\btgil\.hermes` root:
- moved conflicting paths to `C:\Users\btgil\.hermes\_shadow_backup_20260316_172529`.
- File refs:
- `cli.py`
- `run_agent.py`
- `tools/browser_tool.py`
- `tests/tools/test_browser_windows_stream_port.py`
- Validation:
- `python -m py_compile tools/browser_tool.py tests/tools/test_browser_windows_stream_port.py run_agent.py cli.py` passed.
- `pytest -q -o addopts='' tests/tools/test_browser_windows_stream_port.py` passed (`8 passed`).
- Live uv-tool runtime probe from `C:\Users\btgil\.hermes`:
- `_run_browser_command(... open https://github.com ...)` returned success in ~`1.16s`.

## Merge Safety Rules
- Keep upstream `main` behavior as baseline.
- Port integrations in small slices with compile/smoke validation per slice.
- Record each applied file set and verification result in this ledger.
- Treat large divergent files (`gateway/run.py`, `cli.py`, `gateway/platforms/discord.py`, `hermes_cli/gateway.py`) as surgical merge targets, not whole-file replacements.
