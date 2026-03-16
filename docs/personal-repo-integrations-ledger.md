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
- Current code compare signal:
- `gateway/platforms/discord.py` is heavily divergent vs personal repo (`813 insertions, 1343 deletions` in no-index compare).
- `cli.py` is heavily divergent vs personal repo (`995 insertions, 4061 deletions` in no-index compare).
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

## Merge Safety Rules
- Keep upstream `main` behavior as baseline.
- Port integrations in small slices with compile/smoke validation per slice.
- Record each applied file set and verification result in this ledger.
- Treat large divergent files (`gateway/run.py`, `cli.py`, `gateway/platforms/discord.py`, `hermes_cli/gateway.py`) as surgical merge targets, not whole-file replacements.
